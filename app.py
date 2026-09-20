import os,json,sys
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask,jsonify,request
load_dotenv();app=Flask(__name__)
A=Path(__file__).resolve().parent/'autonomy'
if not A.is_dir(): A=A.parent/'autonomy'
sys.path.insert(0,str(A))

def parse(p):
 s=os.getenv('STRIPE_WEBHOOK_SECRET','').strip()
 if s:
  import stripe
  h=request.headers.get('Stripe-Signature')
  if not h: raise ValueError('Missing Stripe-Signature header')
  e=stripe.Webhook.construct_event(p,h,s)
  return e.to_dict() if hasattr(e,'to_dict') else dict(e)
 if os.getenv('DEV_MODE','').lower() not in ('1','true','yes'): raise PermissionError('Set STRIPE_WEBHOOK_SECRET or DEV_MODE=1')
 e=json.loads(p.decode())
 if not isinstance(e,dict): raise ValueError('Event JSON must be an object')
 return e

@app.get('/health')
def health(): return jsonify(ok=True)
@app.get('/')
def index(): return 'ServerDesk webhook; /health; /webhooks/stripe\\n'
@app.post('/webhooks/stripe')
def webhook():
 try: e=parse(request.get_data())
 except PermissionError as x: return jsonify(error=str(x)),403
 except Exception as x: return jsonify(error=str(x)),400
 if e.get('type')!='checkout.session.completed': return jsonify(ok=True,ignored=e.get('type'))
 o=(e.get('data') or {}).get('object') or {};d=o.get('customer_details') or {}
 email=d.get('email') if isinstance(d,dict) else None
 email=email or o.get('customer_email')
 if not email:return jsonify(error='checkout session missing customer email'),400
 from provision import write_provision_packet
 m=o.get('metadata') or {};p=write_provision_packet(email=str(email),guild_id=m.get('guild_id'),session_id=o.get('id'))
 return jsonify(ok=True,provisioned=True,email=email,session_id=o.get('id'),packet=str(p))

if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.getenv('PORT','8790')))
