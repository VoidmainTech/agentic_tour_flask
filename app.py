import os, json, sqlite3, threading, smtplib, re
from datetime import datetime, date, timedelta
from pathlib import Path
from email.message import EmailMessage
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
import requests
from google import genai
from google.genai import types

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
DB = BASE / 'tour_agent.sqlite'
app = Flask(__name__)
MODEL = 'gemini-3.5-flash-lite'
ACTIVITIES = [
 ('Baga Beach Walk','beach','North Goa',1.5,0,1,0,'Sunset shoreline walk; weather-dependent'),
 ('Fort Aguada Viewpoint','heritage','North Goa',1.5,50,1,0,'Historic fort and coastal viewpoint'),
 ('Museum of Goa','museum','North Goa',1.5,250,0,1,'Indoor art museum; confirm hours before visiting'),
 ('Goa State Museum','museum','Panaji',1.5,100,0,1,'Indoor history and heritage; verify opening'),
 ('Fontainhas Heritage Walk','heritage','Panaji',1.5,100,1,0,'Panaji heritage neighborhood'),
 ('Indoor Art Cafe','food','Panaji',1.0,450,0,1,'Illustrative indoor meal stop'),
 ('Local Goan Restaurant','food','North Goa',1.0,550,0,1,'Illustrative meal stop'),
 ('Miramar Beach','beach','Panaji',1.0,0,1,0,'Open-air coastline; weather-dependent')]

def db():
    c=sqlite3.connect(DB,timeout=20)
    c.row_factory=sqlite3.Row
    return c

def setup():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS attractions(name TEXT PRIMARY KEY,category TEXT,area TEXT,duration_hours REAL,price_per_person INTEGER,outdoor INTEGER,rain_safe INTEGER,description TEXT);
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT,goal TEXT,status TEXT,request_json TEXT,plan_json TEXT,validation_json TEXT,weather_json TEXT,wiki_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER,stage TEXT,message TEXT,details_json TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS saved_plans(run_id INTEGER PRIMARY KEY,plan_json TEXT NOT NULL,approved_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS email_results(run_id INTEGER PRIMARY KEY,recipient TEXT,status TEXT,message TEXT,created_at TEXT);''')
        c.executemany('INSERT OR REPLACE INTO attractions VALUES(?,?,?,?,?,?,?,?)',ACTIVITIES)
setup()

def now(): return datetime.now().isoformat(timespec='seconds')
def event(run_id,stage,message,details=None):
    with db() as c:
        c.execute('INSERT INTO events(run_id,stage,message,details_json,created_at) VALUES(?,?,?,?,?)',(run_id,stage,str(message)[:1200],json.dumps(details,ensure_ascii=False,default=str) if details is not None else None,now()))

def status(run_id, value, **fields):
    keys=['status']+list(fields)
    vals=[value]+[json.dumps(fields[k],ensure_ascii=False) for k in fields]
    with db() as c:
        c.execute('UPDATE runs SET '+','.join(k+'=?' for k in keys)+' WHERE id=?',(*vals,run_id))

def fetch_json(url,params):
    r=requests.get(url,params=params,timeout=15,headers={'User-Agent':'VoidmainTourPlannerDemo/1.0 (learning project)'})
    r.raise_for_status();return r.json()

def weather_tool(city,travel_date):
    geo=fetch_json('https://geocoding-api.open-meteo.com/v1/search',{'name':city,'count':5,'language':'en','format':'json'})
    choices=geo.get('results') or []
    if not choices:return {'ok':False,'error':'Location could not be geocoded'}
    loc=next((p for p in choices if p.get('country_code')=='IN'),choices[0])
    data=fetch_json('https://api.open-meteo.com/v1/forecast',{'latitude':loc['latitude'],'longitude':loc['longitude'],'daily':'precipitation_probability_max,precipitation_sum,temperature_2m_max','timezone':loc.get('timezone','auto'),'forecast_days':7})
    daily=data.get('daily') or {}; dates=daily.get('time') or []
    if travel_date not in dates:return {'ok':False,'error':'Selected date outside available forecast window','available_dates':dates}
    i=dates.index(travel_date); prob=(daily.get('precipitation_probability_max') or [])[i] or 0; mm=(daily.get('precipitation_sum') or [])[i] or 0
    return {'ok':True,'city':loc['name'],'date':travel_date,'rain_probability_percent':prob,'rain_mm':mm,'max_temp_c':(daily.get('temperature_2m_max') or [None])[i], 'rain_risk':'HIGH' if prob>=65 or mm>=10 else ('MODERATE' if prob>=35 else 'LOW'),'source':'Open-Meteo weather forecast; not a guarantee'}

def wiki_tool(topic):
    data=fetch_json('https://en.wikipedia.org/w/api.php',{'action':'query','prop':'extracts','exintro':1,'explaintext':1,'redirects':1,'titles':topic,'format':'json'})
    page=next(iter((data.get('query',{}).get('pages') or {}).values()),{})
    if 'missing' in page or not page:return {'ok':False,'error':'No Wikipedia article located'}
    title=page.get('title',topic)
    return {'ok':True,'title':title,'summary':page.get('extract','')[:900],'url':'https://en.wikipedia.org/wiki/'+title.replace(' ','_')}

def activities_tool():
    with db() as c:rows=[dict(r) for r in c.execute('SELECT * FROM attractions ORDER BY area,name')]
    return {'ok':True,'disclaimer':'Demonstration-only Goa catalog; prices and opening hours are not live','activities':rows}

def ai_json(client,prompt):
    answer=client.models.generate_content(model=MODEL,contents=prompt,config=types.GenerateContentConfig(response_mime_type='application/json',temperature=0.3))
    return json.loads(answer.text)

def validate_plan(plan,catalog,people,budget,rain):
    problems=[]; stops=plan.get('stops',[])
    if not isinstance(stops,list): stops=[]
    if not 2<=len(stops)<=5:problems.append('Must contain 2–5 stops')
    names=[s.get('name') for s in stops if isinstance(s,dict)]
    if len(names)!=len(stops):problems.append('Malformed stop')
    if len(names)!=len(set(names)):problems.append('Duplicate stops')
    if any(n not in catalog for n in names):problems.append('Unknown attraction; only allowlisted catalog items permitted')
    if rain in ('HIGH','UNKNOWN') and any(catalog.get(n,{}).get('outdoor') for n in names):problems.append('Outdoor stops prohibited in high or unknown rain conditions')
    if not any(catalog.get(n,{}).get('category')=='food' for n in names):problems.append('Must include a food stop')
    transport=plan.get('transport_total_inr')
    if type(transport) is not int or not 0<=transport<=3500:problems.append('Transport cost must be integer between 0 and 3500')
    activity=sum(catalog[n]['price_per_person']*people for n in names if n in catalog)
    total=activity+(transport if type(transport) is int else 0)
    if total>budget:problems.append(f'Over budget: ₹{total:,} > ₹{budget:,}')
    hours=sum(catalog[n]['duration_hours'] for n in names if n in catalog)
    if hours>9:problems.append('Activities exceed 9 hours (excluding transfers)')
    for s in stops:
        if not isinstance(s,dict) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',str(s.get('start_time',''))):problems.append('Invalid or missing start time');break
    return {'valid':not problems,'errors':problems,'total_estimate_inr':total,'activity_cost_inr':activity,'transport_estimate_inr':transport,'duration_hours_excluding_transfers':hours,'note':'Illustrative costs only; verify opening hours, availability and actual taxi charges'}

def run_agent(run_id,req):
    try:
        key=os.getenv('GEMINI_API_KEY', '').strip()
        if not key:raise RuntimeError('GEMINI_API_KEY is missing. Configure your .env file.')
        client=genai.Client(api_key=key)
        city=req['destination']; dt=req['travel_date']; people=req['people']; budget=req['budget_inr']
        goal=f'Plan a one-day Goa trip for {people} on {dt}, city {city}, within INR {budget}, considering weather and illustrative local costs.'
        event(run_id,'GOAL','Received trip request',req)
        if city.strip().lower() not in ('goa','goa, india'):
            raise ValueError('This notebook uses a Goa-only SQLite attraction catalog. Destination must be Goa; another destination would require a verified new catalog.')
        tools={'weather_tool':lambda:weather_tool(city,dt),'wiki_tool':lambda:wiki_tool('Goa'),'activities_tool':activities_tool}
        collected={}
        event(run_id,'RESEARCH','Starting model-directed read-only tool selection')
        for i in range(3):
            remaining=[t for t in tools if t not in collected]
            prompt=f'You are a travel research agent. Pick exactly one next useful tool from {remaining}. Already completed: {list(collected)}. Goal: {goal}. Reply JSON with tool and reason. Never choose a completed tool.'
            try:
                choice=ai_json(client,prompt); tool=choice.get('tool')
                if tool not in remaining:raise ValueError('Model selected unapproved or repeated tool')
                reason=choice.get('reason','')
            except Exception as ex:
                tool=remaining[0];reason=f'Safe read-only selection fallback ({type(ex).__name__})'
                event(run_id,'DECISION FALLBACK','Model tool choice invalid; fallback to allowlisted tool')
            event(run_id,'DECIDE',f'Step {i+1}: chose {tool}',{'reason':reason,'remaining':remaining,'allowed':True})
            event(run_id,'ACT',f'Calling {tool}')
            try:
                collected[tool]=tools[tool]()
                event(run_id,'OBSERVE',f'{tool} returned',collected[tool])
            except Exception as ex:
                collected[tool]={'ok':False,'error':str(ex)[:250]}
                event(run_id,'TOOL FAILURE',f'{tool} failed; continue conservatively',collected[tool])
        weather=collected.get('weather_tool') or {};rain=weather.get('rain_risk','UNKNOWN') if weather.get('ok') else 'UNKNOWN'
        if rain=='UNKNOWN':event(run_id,'SAFETY','Forecast missing; indoor activities only')
        catalog={a['name']:a for a in collected.get('activities_tool',{}).get('activities',[])}
        if not catalog:catalog={a['name']:a for a in activities_tool()['activities']};event(run_id,'CATALOG FALLBACK','Loaded local allowlisted catalog')
        plan=None; verdict={'errors':[]}
        for attempt in range(1,5):
            event(run_id,'PLAN' if attempt==1 else 'REPLAN',f'Generating proposal, attempt {attempt}/4',{'previous_errors':verdict['errors'],'rain_risk':rain})
            prompt=f'''Create a one-day Goa itinerary for {people} people on {dt}, budget ₹{budget}. Allowed stops ONLY: {json.dumps(list(catalog.values()))}. Weather: {json.dumps(weather)}. Rain risk: {rain}. If HIGH or UNKNOWN, use ONLY indoor locations (outdoor=0). Select 2-5 distinct stops, including one food stop. Group attractions geographically. Each attraction has price_per_person. Add transport_total_inr integer 600-2500, total <= {budget}. Return JSON object containing title (str), rationale (str), stops (array of objects with name, start_time in HH:MM, why), transport_total_inr (int), assumptions (array of strings). Costs are estimates, never promise bookings or verified hours. Previous validation errors: {json.dumps(verdict['errors'])}'''
            try:
                proposal=ai_json(client,prompt)
                verdict=validate_plan(proposal,catalog,people,budget,rain)
                event(run_id,'VALIDATE','Proposal accepted' if verdict['valid'] else 'Proposal rejected by deterministic checks',{'attempt':attempt,**verdict,'proposed_stops':[s.get('name') for s in proposal.get('stops',[]) if isinstance(s,dict)]})
                if verdict['valid']:plan=proposal;break
            except Exception as ex:
                verdict={'errors':[f'{type(ex).__name__}: {str(ex)[:150]}']}
                event(run_id,'RETRY','Generation/JSON error; retrying',verdict)
        if plan is None:
            event(run_id,'SAFE FALLBACK','AI proposals failed; building conservative allowed plan')
            available=[a for a in catalog.values() if not a['outdoor']]
            picks=[a for a in available if a['category']=='museum'][:1]+[a for a in available if a['category']=='food'][:1]
            if rain not in ('HIGH','UNKNOWN'):picks += [a for a in catalog.values() if a['category']=='heritage' and a['outdoor']][:1]
            plan={'title':'Conservative Goa day outing','rationale':'Rule-based fallback after proposal validation failures','stops':[{'name':a['name'],'start_time':t,'why':a['description']} for a,t in zip(picks,['10:00','12:30','15:00'])],'transport_total_inr':min(1200,max(0,budget)),'assumptions':['Illustrative prices; confirm all logistics']}
            verdict=validate_plan(plan,catalog,people,budget,rain)
            event(run_id,'FALLBACK VALIDATE','Fallback validation result',verdict)
        if not verdict['valid']:raise RuntimeError('Unable to create a plan within safety and budget constraints: '+('; '.join(verdict['errors'])))
        event(run_id,'PERMISSION','Valid plan ready. Awaiting explicit approval before database write or email.')
        status(run_id,'awaiting_approval',plan_json=plan,validation_json=verdict,weather_json=weather,wiki_json=collected.get('wiki_tool',{}))
    except Exception as ex:
        event(run_id,'ERROR',str(ex)[:600]);status(run_id,'failed')

def get_run(run_id):
    with db() as c:
        row=c.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:return None
        res=dict(row)
        for k in ('request_json','plan_json','validation_json','weather_json','wiki_json'):
            res[k]=json.loads(res[k]) if res[k] else None
        res['events']=[{'id':e['id'],'stage':e['stage'],'message':e['message'],'details':json.loads(e['details_json']) if e['details_json'] else None,'at':e['created_at']} for e in c.execute('SELECT * FROM events WHERE run_id=? ORDER BY id',(run_id,))]
        email=c.execute('SELECT recipient,status,message FROM email_results WHERE run_id=?',(run_id,)).fetchone();res['email']=dict(email) if email else None
        return res

@app.get('/')
def index():return render_template('index.html',default_date=(date.today()+timedelta(days=2)).isoformat())

@app.post('/api/runs')
def create():
    data=request.get_json(silent=True) or {}
    try:
        destination=str(data.get('destination','Goa')).strip()
        if destination.lower() not in ('goa','goa, india'):raise ValueError('This demo supports Goa only because the notebook catalog contains Goa attractions.')
        trip_date=date.fromisoformat(str(data['travel_date']))
        if not date.today()<=trip_date<=date.today()+timedelta(days=6):raise ValueError('Select a date from today through the next 6 days (forecast window).')
        people=int(data['people']);budget=int(data['budget_inr']);recipient=str(data.get('email','')).strip()
        if not 1<=people<=10:raise ValueError('Number of travelers must be 1–10')
        if not 1000<=budget<=100000:raise ValueError('Budget must be ₹1,000–₹100,000')
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',recipient):raise ValueError('Enter a valid recipient email')
        req={'destination':'Goa','travel_date':trip_date.isoformat(),'people':people,'budget_inr':budget,'email':recipient}
        with db() as c:
            cur=c.execute('INSERT INTO runs(goal,status,request_json,created_at) VALUES(?,?,?,?)',('Goa tour planning','running',json.dumps(req),now()));rid=cur.lastrowid
        threading.Thread(target=run_agent,args=(rid,req),daemon=True).start()
        return jsonify({'run_id':rid}),202
    except (KeyError,TypeError,ValueError) as ex:return jsonify({'error':str(ex)}),400

@app.get('/api/runs/<int:run_id>')
def run_detail(run_id):
    record=get_run(run_id)
    return jsonify(record) if record else (jsonify({'error':'Run not found'}),404)

def send_email(recipient,subject,body):
    sender = os.getenv('SENDER_GMAIL', '').strip()
    password = os.getenv('GMAIL_APP_PASSWORD', '').strip()
    if not sender or not password:raise RuntimeError('Email not configured. Set SENDER_GMAIL and GMAIL_APP_PASSWORD in .env')
    msg=EmailMessage();msg['From']=sender;msg['To']=recipient;msg['Subject']=subject;msg.set_content(body)
    with smtplib.SMTP_SSL('smtp.gmail.com',465,timeout=25) as smtp:
        smtp.login(sender,password);smtp.send_message(msg)

def email_body(r):
    p=r['plan_json']; v=r['validation_json']; q=r['request_json']
    lines=[p['title'],f"Date: {q['travel_date']}",f"Travelers: {q['people']}",f"Budget: INR {q['budget_inr']}",f"Estimated total: INR {v['total_estimate_inr']}",'','Itinerary:']
    lines.extend(f"{i}. {s['start_time']} - {s['name']} - {s.get('why','')}" for i,s in enumerate(p['stops'],1))
    lines.extend(['',f"Transport estimate: INR {v['transport_estimate_inr']}",'',p.get('rationale',''),'','Estimates only. This is not a booking confirmation. Confirm hours, transport and availability independently.'])
    return '\n'.join(lines)

@app.post('/api/runs/<int:run_id>/decision')
def decide(run_id):
    payload=request.get_json(silent=True) or {}; action=payload.get('action')
    if action not in ('approve','reject'):return jsonify({'error':'Use approve or reject'}),400
    with db() as c:
        # SQLite write lock ensures only one approval/rejection operation wins.
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:return jsonify({'error':'Not found'}),404
        if row['status']!='awaiting_approval':return jsonify({'error':'This run has already been processed or is not ready'}),409
        if action=='reject':
            c.execute('UPDATE runs SET status=? WHERE id=?',('rejected',run_id))
            c.commit()
            event(run_id,'REJECTED','User declined; nothing saved or emailed')
            return jsonify({'status':'rejected','message':'Plan rejected; no email was sent'})
        r=get_run(run_id)
        plan=r['plan_json'];req=r['request_json']
        catalog={a['name']:a for a in activities_tool()['activities']}
        rain=(r['weather_json'] or {}).get('rain_risk','UNKNOWN')
        verdict=validate_plan(plan,catalog,req['people'],req['budget_inr'],rain)
        if not verdict['valid']:return jsonify({'error':'Final validation failed','details':verdict['errors']}),422
        serialized=json.dumps({'request':req,'plan':plan,'validation':verdict,'weather':r['weather_json']},ensure_ascii=False)
        c.execute('INSERT INTO saved_plans(run_id,plan_json,approved_at) VALUES(?,?,?)',(run_id,serialized,now()))
        stored=c.execute('SELECT plan_json FROM saved_plans WHERE run_id=?',(run_id,)).fetchone()
        if not stored or stored['plan_json']!=serialized:raise RuntimeError('SQLite read-back failed')
        c.execute('UPDATE runs SET status=? WHERE id=?',('saved',run_id))
    event(run_id,'APPROVED','Human approved the validated plan')
    event(run_id,'VERIFIED','Plan written to SQLite and checked by read-back')
    # Sending the email is an explicitly authorized external action; do it after save/verification.
    event(run_id,'EMAIL SEND','Attempting SMTP delivery to authorized recipient')
    r=get_run(run_id); recipient=r['request_json']['email']
    try:
        send_email(recipient,'Your Goa Tour Plan Confirmation',email_body(r))
        result='sent'; message='SMTP server accepted the email for delivery.'
        status(run_id,'email_sent')
        event(run_id,'EMAIL SENT',message,{'recipient':recipient})
    except Exception as ex:
        result='failed';message=f'{type(ex).__name__}: {str(ex)[:220]}'
        status(run_id,'email_failed')
        event(run_id,'EMAIL FAILED','Plan remains saved, but email delivery failed',{'error':message})
    with db() as c:c.execute('INSERT OR REPLACE INTO email_results(run_id,recipient,status,message,created_at) VALUES(?,?,?,?,?)',(run_id,recipient,result,message,now()))
    return jsonify({'status':result,'message':message,'plan_saved':True})

if __name__=='__main__':
        app.run(host='127.0.0.1',port=5000,debug=False)