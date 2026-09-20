import json,re
from datetime import datetime,timezone
from pathlib import Path
from paths import PROVISIONS_DIR,INVITE_URL_FILE,STRIPE_LINK_FILE,LANDING_URL_FILE,ensure_data_dirs

def read(p,default=''):
    try:return p.read_text().strip()
    except OSError:return default

def write_provision_packet(email,guild_id=None,session_id=None,out_dir=PROVISIONS_DIR):
    if not email or '@' not in email: raise ValueError('A valid paid email is required')
    ensure_data_dirs();out_dir.mkdir(parents=True,exist_ok=True)
    ts=datetime.now(timezone.utc); stamp=ts.strftime('%Y%m%dT%H%M%SZ')
    slug=re.sub(r'[^a-zA-Z0-9@.+-]+','_',email.strip().lower())[:80]
    base=stamp+'_'+(slug or 'unknown')
    if session_id: base+='_'+str(session_id)[:16]
    invite=read(INVITE_URL_FILE,'https://discord.com/oauth2/authorize?scope=bot%20applications.commands')
    landing=read(LANDING_URL_FILE); stripe=read(STRIPE_LINK_FILE)
    packet={'email':email.strip(),'guild_id':guild_id,'session_id':session_id,'created_at':ts.isoformat(),'invite_url':invite,'landing_url':landing,'stripe_payment_link':stripe,'bot':'ServerDesk#6194','notes':'Deliver manually; no auto-DM.'}
    (out_dir/(base+'.json')).write_text(json.dumps(packet,indent=2)+'\n')
    md=f"# ServerDesk provision packet\n\n- Customer email: {email}\n- Guild ID: {guild_id or '(not provided)'}\n- Stripe session: {session_id or '(manual)'}\n\n## Invite URL\n\n```\n{invite}\n```\n\nRun `/setup` in Discord after inviting the bot.\n\nLanding: {landing}\nBilling: {stripe}\n"
    path=out_dir/(base+'.md');path.write_text(md);return path
