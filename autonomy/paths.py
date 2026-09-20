from pathlib import Path
AUTONOMY_ROOT=Path(__file__).resolve().parent
SERVERDESK_ROOT=AUTONOMY_ROOT.parent
DATA_DIR=AUTONOMY_ROOT/'data'
PROVISIONS_DIR=DATA_DIR/'provisions'
INVITE_URL_FILE=SERVERDESK_ROOT/'INVITE_URL.txt'
STRIPE_LINK_FILE=SERVERDESK_ROOT/'STRIPE_PAYMENT_LINK.txt'
LANDING_URL_FILE=SERVERDESK_ROOT/'LANDING_URL.txt'
def ensure_data_dirs():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    PROVISIONS_DIR.mkdir(parents=True,exist_ok=True)
