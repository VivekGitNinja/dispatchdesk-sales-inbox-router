"""DispatchDesk — single-file Vercel serverless backend."""
import os, sys, datetime, uuid, re, json, logging
from typing import Optional, Dict, List, Any
from enum import Enum
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import (Boolean, Column, DateTime, Float, Integer, String, Text,
                        UniqueConstraint, create_engine, func, or_)
from sqlalchemy.orm import declarative_base, sessionmaker
from pydantic import BaseModel, Field

class Category(str, Enum):
    ENTERPRISE_RFP="enterprise_rfp"; SMB_ENQUIRY="smb_enquiry"; MARKETING="marketing"
    ALLIANCES="alliances"; FINANCE="finance"; TRIAGE="triage"
    SKIP_AUTO_REPLY="skip_auto_reply"; SKIP_NEWSLETTER="skip_newsletter"
    SKIP_VENDOR_SPAM="skip_vendor_spam"; SKIP_OTHER="skip_other"
class Priority(str, Enum):
    URGENT="urgent"; HIGH="high"; MEDIUM="medium"; LOW="low"
class TargetRole(str, Enum):
    FOUNDER_OPS="FOUNDER_OPS"; SALES_TEAM="SALES_TEAM"; SUPPORT_TEAM="SUPPORT_TEAM"
    FINANCE_TEAM="FINANCE_TEAM"; NONE="NONE"
CATEGORIES=[c.value for c in Category if not c.value.startswith("skip_")]
PRIORITIES=[p.value for p in Priority]
ASSIGNEE_IDS=["u_aarti","u_rohit","u_meera","u_karan","u_divya","u_triage"]
ROLE_MAP={Category.ENTERPRISE_RFP:TargetRole.FOUNDER_OPS,Category.SMB_ENQUIRY:TargetRole.SALES_TEAM,
          Category.MARKETING:TargetRole.SALES_TEAM,Category.ALLIANCES:TargetRole.SALES_TEAM,
          Category.FINANCE:TargetRole.FINANCE_TEAM,Category.TRIAGE:TargetRole.SUPPORT_TEAM,
          Category.SKIP_AUTO_REPLY:TargetRole.NONE,Category.SKIP_NEWSLETTER:TargetRole.NONE,
          Category.SKIP_VENDOR_SPAM:TargetRole.NONE,Category.SKIP_OTHER:TargetRole.NONE}
def role_for_cat(c):
    return ROLE_MAP.get(c,TargetRole.SUPPORT_TEAM) if c else TargetRole.SUPPORT_TEAM
def normalize_cid(v):
    return str(v or "").strip().lower()
class RoutingDecision(BaseModel):
    action:str="create_task";skip_reason:Optional[str]=None;category:Optional[Category]=None
    assignee_id:Optional[str]=None;priority:Priority=Priority.MEDIUM;due_date:Optional[str]=None
    deal_value_inr:Optional[int]=None;company_name:Optional[str]=None
    confidence:float=Field(default=0.0,ge=0.0,le=1.0);title:str="";description:str=""
    reasoning:str="";target_role:Optional[TargetRole]=None
class ChatRequest(BaseModel):
    candidate_id:Optional[str]=None;query:str

logging.basicConfig(level=logging.INFO)
logger=logging.getLogger(__name__)
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
_tmp="/tmp" if os.path.isdir("/tmp") else BASE_DIR
_default_db="sqlite:///"+os.path.join(_tmp,"inbox_router.db")
DATABASE_URL=os.getenv("DATABASE_URL") or _default_db
CANDIDATE_ID_DEFAULT=(os.getenv("CANDIDATE_ID") or "demo@dispatchdesk.ai").strip().lower()
connect_args={}
if DATABASE_URL.startswith("sqlite"): connect_args["check_same_thread"]=False
if DATABASE_URL.startswith("postgres"): connect_args["sslmode"]=os.getenv("PGSSLMODE","require")
engine=create_engine(DATABASE_URL,connect_args=connect_args,pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)
Base=declarative_base()
class Task(Base):
    __tablename__="tasks"
    task_id=Column(String,primary_key=True);candidate_id=Column(String,index=True)
    source_email_id=Column(String,index=True);thread_id=Column(String,index=True)
    title=Column(Text);description=Column(Text);assignee_id=Column(String)
    category=Column(String);priority=Column(String);due_date=Column(String,nullable=True)
    deal_value_inr=Column(Integer,nullable=True);company_name=Column(String,nullable=True)
    confidence=Column(Float);update_count=Column(Integer,default=0)
    batch_id=Column(String,nullable=True)
    created_at=Column(DateTime,default=datetime.datetime.utcnow)
    updated_at=Column(DateTime,default=datetime.datetime.utcnow,onupdate=datetime.datetime.utcnow)
    __table_args__=(UniqueConstraint("candidate_id","source_email_id",name="uq_cse"),)
class EmailLog(Base):
    __tablename__="email_logs"
    id=Column(Integer,primary_key=True,autoincrement=True);candidate_id=Column(String,index=True)
    batch_id=Column(String,index=True);email_id=Column(String,index=True)
    thread_id=Column(String,index=True);subject=Column(Text);from_name=Column(Text)
    from_email=Column(Text);received_at=Column(String);cleaned_body=Column(Text)
    decision=Column(String);category=Column(String);assignee_id=Column(String)
    priority=Column(String);due_date=Column(String);deal_value_inr=Column(Integer)
    company_name=Column(String);confidence=Column(Float);skip_reason=Column(String)
    reasoning=Column(Text);task_id=Column(String);is_update=Column(Boolean,default=False)
    created_at=Column(DateTime,default=datetime.datetime.utcnow)
    __table_args__=(UniqueConstraint("candidate_id","email_id",name="uq_ce"),)
DB_ERROR=None
try: Base.metadata.create_all(bind=engine)
except Exception as e:
    DB_ERROR=str(e);engine=None
    def SessionLocal(): raise RuntimeError(DB_ERROR)
def new_task_id(): return "tsk_"+uuid.uuid4().hex[:6]
def task_to_dict(t):
    return {"task_id":t.task_id,"candidate_id":t.candidate_id,"source_email_id":t.source_email_id,
            "thread_id":t.thread_id,"title":t.title,"description":t.description,
            "assignee_id":t.assignee_id,"target_role":role_for_cat(t.category).value,
            "category":t.category,"priority":t.priority,"due_date":t.due_date,
            "deal_value_inr":t.deal_value_inr,"company_name":t.company_name,
            "confidence":t.confidence,"update_count":t.update_count,"batch_id":t.batch_id,
            "created_at":t.created_at.isoformat() if t.created_at else None,
            "updated_at":t.updated_at.isoformat() if t.updated_at else None}
def log_to_dict(l):
    return {"email_id":l.email_id,"thread_id":l.thread_id,"subject":l.subject,
            "from_name":l.from_name,"from_email":l.from_email,"received_at":l.received_at,
            "decision":l.decision,"category":l.category,"assignee_id":l.assignee_id,
            "target_role":role_for_cat(l.category).value,"priority":l.priority,
            "due_date":l.due_date,"deal_value_inr":l.deal_value_inr,"company_name":l.company_name,
            "confidence":l.confidence,"skip_reason":l.skip_reason,"reasoning":l.reasoning,
            "task_id":l.task_id,"is_update":l.is_update,
            "created_at":l.created_at.isoformat() if l.created_at else None,
            "body_preview":(l.cleaned_body or "")[:200]}
TEAM={"team":[
    {"user_id":"u_aarti","name":"Aarti Menon","department":"Sales — Enterprise","role":"FOUNDER_OPS","scope":"RFPs, RFIs, tenders"},
    {"user_id":"u_rohit","name":"Rohit Sharma","department":"Sales — SMB","role":"SALES_TEAM","scope":"Product enquiries, demo requests"},
    {"user_id":"u_meera","name":"Meera Iyer","department":"Marketing","role":"SALES_TEAM","scope":"Webinars, sponsorships, events"},
    {"user_id":"u_karan","name":"Karan Doshi","department":"Alliances","role":"SALES_TEAM","scope":"Reseller, channel partner proposals"},
    {"user_id":"u_divya","name":"Divya Rao","department":"Finance","role":"FINANCE_TEAM","scope":"Invoices, purchase orders, payments"},
    {"user_id":"u_triage","name":"Triage Queue","department":"Operations","role":"SUPPORT_TEAM","scope":"Ambiguous items"},
]}
_SPAM={"unsubscribe","newsletter","weekly digest","monthly roundup","press release"}
_AUTO={"out of office","auto-reply","automatic reply","i am currently away","limited access"}
_VENDOR={"seo audit","free consultation","guest post","link building","backlink","cold outreach"}
def clean_body(b):
    if not b: return ""
    return "\n".join(l.strip() for l in b.split("\n") if l.strip() and not l.strip().startswith(">") and not any(k in l.lower() for k in ["unsubscribe","view in browser","email preferences"]))
def parse_inr(t):
    if not t: return None
    for p in [r"₹\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|cr|crore)?",r"Rs\.?\s*([\d,]+(?:\.\d+)?)\s*(lakh|lac|cr|crore)?",r"([\d,]+(?:\.\d+)?)\s*(lakh|lac|cr|crore)"]:
        m=re.search(p,t,re.IGNORECASE)
        if m:
            v=float(m.group(1).replace(",",""));s=(m.group(2) or "").lower()
            if s in ("lakh","lac"): v*=100000
            elif s in ("cr","crore"): v*=10000000
            return int(v)
    return None
def extract_date(t):
    if not t: return None
    m=re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})",t)
    if m: return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return None
def extract_company(t,fe):
    if not t: return None
    m=re.search(r"(?:from|at|of)\s+([A-Z][A-Za-z\s&]+(?:Ltd|Inc|Corp|Pvt|LLC|Co))",t)
    if m: return m.group(1).strip()
    if fe:
        d=fe.split("@")[-1].split(".")[0]
        if d not in ("gmail","yahoo","hotmail","outlook"): return d.title()
    return None
def fallback_analyze(email,cleaned):
    text=f"{email.get('subject','')} {cleaned}".lower()
    body=f"{email.get('subject','')} {cleaned}"
    fe=email.get("from_email","")
    if any(k in text for k in _AUTO): return {"action":"skip","skip_reason":"auto_reply","category":"skip_auto_reply","confidence":0.9}
    if any(k in text for k in _SPAM): return {"action":"skip","skip_reason":"newsletter","category":"skip_newsletter","confidence":0.85}
    if any(k in text for k in _VENDOR): return {"action":"skip","skip_reason":"vendor_spam","category":"skip_vendor_spam","confidence":0.8}
    im=["and also","additionally","two things","multiple requests","both"]
    if sum(1 for m2 in im if m2 in text)>=1:
        return {"action":"create_task","category":"triage","assignee_id":"u_triage","priority":"medium","confidence":0.3,"title":email.get("subject","Two asks"),"description":cleaned[:500],"reasoning":"Multi-intent"}
    dv=parse_inr(body);dd=extract_date(body);co=extract_company(body,fe)
    iu=any(w in text for w in ["urgent","asap","immediately","deadline","overdue","board review"])
    if "tender" in text or "psu" in text or "government" in text or "procurement" in text:
        ca,as2="enterprise_rfp","u_aarti";pr="urgent"
    elif any(w in text for w in ["rfp","proposal","enterprise","budget"]) and dv and dv>1000000:
        ca,as2="enterprise_rfp","u_aarti";pr="high" if iu else "medium"
    elif any(w in text for w in ["demo","enquiry","trial","pricing","how much","cost"]):
        ca,as2="smb_enquiry","u_rohit";pr="low"
    elif any(w in text for w in ["sponsorship","webinar","marketing","event","conference"]):
        ca,as2="marketing","u_meera";pr="high" if iu else "medium"
    elif any(w in text for w in ["partner","resell","integration","alliance"]):
        ca,as2="alliances","u_karan";pr="medium"
    elif any(w in text for w in ["invoice","payment","billing","gst","purchase order"]):
        ca,as2="finance","u_divya";pr="high" if iu or "overdue" in text else "medium"
    else: ca,as2="triage","u_triage";pr="medium"
    if iu and pr in ("medium","low"): pr="urgent"
    return {"action":"create_task","category":ca,"assignee_id":as2,"priority":pr,"confidence":0.7,"due_date":dd,"deal_value_inr":dv,"company_name":co,"title":email.get("subject",""),"description":cleaned[:500],"reasoning":f"fallback:{ca}"}
async def analyze_email(email,cleaned,existing_task=None):
    gk=os.getenv("GEMINI_API_KEY")
    if not gk: return fallback_analyze(email,cleaned)
    try:
        import httpx
        m=os.getenv("GEMINI_MODEL","gemini-1.5-flash")
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={gk}"
        p=f"Classify this sales email as JSON. Categories:{CATEGORIES}. Assignees:u_aarti(enterprise),u_rohit(SMB),u_meera(marketing),u_karan(alliances),u_divya(finance),u_triage(ambiguous).Subject:{email.get('subject','')},From:{email.get('from_email','')},Body:{cleaned[:1000]}"
        async with httpx.AsyncClient(timeout=15) as c:
            r=await c.post(url,json={"contents":[{"parts":[{"text":p}]}]})
            if r.status_code==200:
                t=r.json()["candidates"][0]["content"]["parts"][0]["text"]
                m2=re.search(r"\{.*\}",t,re.DOTALL)
                if m2:
                    d=json.loads(m2.group())
                    rd=RoutingDecision(**{k:v for k,v in d.items() if k in RoutingDecision.model_fields})
                    return {**rd.model_dump(exclude_none=True),"category":rd.category.value if rd.category else None,"priority":rd.priority.value}
    except Exception as e: logger.warning(f"Gemini:{e}")
    return fallback_analyze(email,cleaned)
def build_chat_answer(db,cid,q):
    ql=q.lower()
    pr=db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id==cid).scalar() or 0
    tk=db.query(func.count(Task.task_id)).filter(Task.candidate_id==cid).scalar() or 0
    sk=db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id==cid,EmailLog.decision=="skipped").scalar() or 0
    cc=dict(db.query(Task.category,func.count(Task.task_id)).filter(Task.candidate_id==cid).group_by(Task.category).all())
    st={"processed":int(pr),"tasks":int(tk),"skipped":int(sk),"categories":cc}
    if any(w in ql for w in ["how many","count","total"]):
        if "rfp" in ql or "enterprise" in ql: c2=cc.get("enterprise_rfp",0);return f"Enterprise RFPs: {c2}",{**st,"enterprise_rfp":c2}
        if "skip" in ql or "noise" in ql: return f"Skipped: {sk} of {pr} ({round(sk/pr*100) if pr else 0}%)",st
        return f"Total: {pr} emails, {tk} tasks, {sk} skipped",st
    return f"I found {pr} processed emails with {tk} tasks across {len(cc)} categories.",st
async def phrase_chat(q,sa,da):
    gk=os.getenv("GEMINI_API_KEY")
    if not gk: return da
    try:
        import httpx
        m=os.getenv("GEMINI_MODEL","gemini-1.5-flash")
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={gk}"
        async with httpx.AsyncClient(timeout=10) as c:
            r=await c.post(url,json={"contents":[{"parts":[{"text":f"Rephrase concisely: {da}. Data: {json.dumps(sa)}"}]}]})
            if r.status_code==200: return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except: pass
    return da
app=FastAPI(title="DispatchDesk")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=False,allow_methods=["*"],allow_headers=["*"])
@app.get("/health")
def health(): return {"status":"ok","candidate_id":CANDIDATE_ID_DEFAULT}
@app.get("/users")
def users(): return TEAM
@app.get("/api/stats")
def api_stats(candidate_id=None):
    cid=normalize_cid(candidate_id or CANDIDATE_ID_DEFAULT)
    with SessionLocal() as db:
        pr=db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id==cid).scalar() or 0
        tc=db.query(func.count(Task.task_id)).filter(Task.candidate_id==cid).scalar() or 0
        tu=db.query(func.coalesce(func.sum(Task.update_count),0)).filter(Task.candidate_id==cid).scalar() or 0
        sk=db.query(func.count(EmailLog.id)).filter(EmailLog.candidate_id==cid,EmailLog.decision=="skipped").scalar() or 0
        cc=dict(db.query(Task.category,func.count(Task.task_id)).filter(Task.candidate_id==cid).group_by(Task.category).all())
        sc=dict(db.query(EmailLog.skip_reason,func.count(EmailLog.id)).filter(EmailLog.candidate_id==cid,EmailLog.decision=="skipped").group_by(EmailLog.skip_reason).all())
        bb=dict(db.query(EmailLog.batch_id,func.count(EmailLog.id)).filter(EmailLog.candidate_id==cid).group_by(EmailLog.batch_id).all())
        sp=db.query(func.count(Task.task_id)).join(EmailLog,Task.source_email_id==EmailLog.email_id).filter(Task.candidate_id==cid,EmailLog.decision!="skipped",or_(EmailLog.category.in_(("skip_auto_reply","skip_newsletter","skip_vendor_spam")))).scalar() or 0
        return {"candidate_id":cid,"processed":int(pr),"tasks_created":int(tc),"tasks_updated":int(tu),"skipped":int(sk),"spurious_flagged":int(sp),"category_counts":cc,"skipped_counts":sc,"by_batch":bb}
@app.get("/api/tasks")
def api_tasks(candidate_id=None):
    cid=normalize_cid(candidate_id or CANDIDATE_ID_DEFAULT)
    with SessionLocal() as db:
        ts=db.query(Task).filter(Task.candidate_id==cid).order_by(Task.created_at.desc()).all()
        sl=db.query(EmailLog).filter(EmailLog.candidate_id==cid,EmailLog.decision=="skipped").order_by(EmailLog.created_at.desc()).limit(500).all()
        return {"candidate_id":cid,"tasks":[task_to_dict(t) for t in ts],"skipped":[log_to_dict(l) for l in sl]}
@app.post("/ingest")
async def ingest(request: Request):
    p=await request.json();cid=normalize_cid(p.get("candidate_id"))
    if not cid: return JSONResponse(status_code=400,content={"error":"missing candidate_id"})
    em=p.get("emails",[]);bid=uuid.uuid4().hex[:8]
    c={"processed":0,"tasks_created":0,"tasks_updated":0,"skipped":0}
    with SessionLocal() as db:
        for e in em:
            eid=e.get("email_id");tid=e.get("thread_id")
            if not eid: continue
            if db.query(EmailLog).filter(EmailLog.candidate_id==cid,EmailLog.email_id==eid).first(): continue
            cl=clean_body(e.get("body"));et=None
            if tid:
                tk=db.query(Task).filter(Task.candidate_id==cid,Task.thread_id==tid).first()
                if tk: et=task_to_dict(tk)
            a=await analyze_email(e,cl,et)
            if et and a.get("action")!="skip": a["action"]="update_task"
            elif e.get("is_reply") and et: a["action"]="update_task"
            lg=EmailLog(candidate_id=cid,batch_id=bid,email_id=eid,thread_id=tid,subject=e.get("subject"),from_name=e.get("from_name"),from_email=e.get("from_email"),received_at=e.get("received_at"),cleaned_body=cl,decision=a.get("action"),category=a.get("category"),assignee_id=a.get("assignee_id"),priority=a.get("priority"),due_date=a.get("due_date"),deal_value_inr=a.get("deal_value_inr"),company_name=a.get("company_name"),confidence=a.get("confidence") or 0.0,skip_reason=a.get("skip_reason"),reasoning=a.get("reasoning"))
            if a.get("action")=="skip": c["skipped"]+=1;lg.decision="skipped"
            elif a.get("action")=="update_task" and et:
                tk=db.query(Task).filter(Task.task_id==et["task_id"]).first()
                for k in ["title","description","assignee_id","category","priority","due_date","deal_value_inr","company_name","confidence"]:
                    if a.get(k) is not None: setattr(tk,k,a.get(k))
                tk.update_count+=1;tk.updated_at=datetime.datetime.utcnow();c["tasks_updated"]+=1;lg.is_update=True;lg.task_id=tk.task_id
            else:
                tk=Task(task_id=new_task_id(),candidate_id=cid,source_email_id=eid,thread_id=tid,title=a.get("title") or e.get("subject"),description=a.get("description"),assignee_id=a.get("assignee_id"),category=a.get("category"),priority=a.get("priority") or "medium",due_date=a.get("due_date"),deal_value_inr=a.get("deal_value_inr"),company_name=a.get("company_name"),confidence=a.get("confidence") or 0.0,batch_id=bid)
                db.add(tk);c["tasks_created"]+=1;lg.task_id=tk.task_id
            c["processed"]+=1;db.add(lg);db.commit()
    return c
@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    cid=normalize_cid(req.candidate_id or CANDIDATE_ID_DEFAULT)
    with SessionLocal() as db:
        a,s=build_chat_answer(db,cid,req.query)
        if not s.get("out_of_scope"): a=await phrase_chat(req.query,s,a)
        else: s={}
        return {"answer":a,"supporting_data":s}
