#!/usr/bin/env python3
"""销售工作台的轻量正式版服务端：SQLite 持久化、文件上传、查询、导出和分析接口。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import csv, hashlib, io, json, mimetypes, os, secrets, sqlite3, time
from urllib import request as http_request, error as http_error

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("SALES_WORKBENCH_DATA_DIR", str(ROOT)))
DB = DATA_DIR / "sales_workbench.sqlite3"
UPLOADS = DATA_DIR / "uploads"
PORT = int(os.getenv("SALES_WORKBENCH_PORT", "4173"))
HOST = os.getenv("SALES_WORKBENCH_HOST", "0.0.0.0")
SESSIONS = {}
BOOTSTRAP_PASSWORD = os.getenv("SALES_WORKBENCH_BOOTSTRAP_PASSWORD") or secrets.token_urlsafe(12)

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL, owner TEXT NOT NULL, cost REAL NOT NULL DEFAULT 0, key_name TEXT, key_phone TEXT, key_title TEXT, other_contact TEXT, other_phone TEXT, note TEXT, active INTEGER NOT NULL DEFAULT 1, UNIQUE(kind,name));
CREATE TABLE IF NOT EXISTS activities (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, organization_id INTEGER NOT NULL, event_date TEXT NOT NULL, event_type TEXT NOT NULL, owner TEXT NOT NULL, cost REAL NOT NULL DEFAULT 0, participants INTEGER NOT NULL DEFAULT 0, wechat INTEGER NOT NULL DEFAULT 0, leads INTEGER NOT NULL DEFAULT 0, deals REAL NOT NULL DEFAULT 0, photos TEXT, note TEXT, created_at TEXT NOT NULL, FOREIGN KEY(organization_id) REFERENCES organizations(id));
CREATE TABLE IF NOT EXISTS followups (id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id INTEGER NOT NULL, follow_date TEXT NOT NULL, wechat INTEGER NOT NULL DEFAULT 0, leads INTEGER NOT NULL DEFAULT 0, deals REAL NOT NULL DEFAULT 0, note TEXT, FOREIGN KEY(activity_id) REFERENCES activities(id));
CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, user_name TEXT NOT NULL, report_date TEXT NOT NULL, payload TEXT NOT NULL, submitted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, UNIQUE(user_name,report_date));
CREATE TABLE IF NOT EXISTS ai_analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, target_key TEXT NOT NULL, report_date TEXT NOT NULL, payload TEXT NOT NULL, manager_note TEXT, updated_at TEXT NOT NULL, UNIQUE(target_key,report_date));
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

def now(): return time.strftime("%Y-%m-%dT%H:%M:%S")
def db():
    conn=sqlite3.connect(DB); conn.row_factory=sqlite3.Row; conn.execute("PRAGMA foreign_keys=ON"); return conn
def clear_legacy_demo_data(c):
    """Remove the original prototype records once, while preserving later user data."""
    if c.execute("SELECT 1 FROM app_meta WHERE key='demo_cleanup_v1'").fetchone():
        return
    demo_names = ("浙江省数字经济协会", "云启科技")
    demo_users = ("wangxiao", "lining", "chenchen", "ayu", "beiyao")
    orgs = c.execute("SELECT id FROM organizations WHERE name IN (?,?)", demo_names).fetchall()
    ids = [row[0] for row in orgs]
    if ids:
        marks = ",".join("?" for _ in ids)
        c.execute(f"DELETE FROM followups WHERE activity_id IN (SELECT id FROM activities WHERE organization_id IN ({marks}))", ids)
        c.execute(f"DELETE FROM activities WHERE organization_id IN ({marks})", ids)
        c.execute(f"DELETE FROM organizations WHERE id IN ({marks})", ids)
    c.execute("DELETE FROM users WHERE username IN (?,?,?,?,?)", demo_users)
    saved = c.execute("SELECT payload FROM app_state WHERE id=1").fetchone()
    if saved:
        try:
            payload = json.loads(saved[0])
            names = {x.get("name") for x in payload.get("associations", []) + payload.get("partners", [])}
            if names.issubset(set(demo_names)) and not payload.get("activities") and not payload.get("reports"):
                c.execute("DELETE FROM app_state WHERE id=1")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('demo_cleanup_v1','done')")

def init():
    UPLOADS.mkdir(exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        clear_legacy_demo_data(c)
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            c.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES(?,?,?,?)",("admin","管理员","admin",hashlib.sha256(BOOTSTRAP_PASSWORD.encode()).hexdigest()))
            print(f"首次启动账号：admin；初始密码：{BOOTSTRAP_PASSWORD}")
def read_json(h):
    n=int(h.headers.get("Content-Length",0)); return json.loads(h.rfile.read(n) or b"{}")
def rows(conn,sql,args=()): return [dict(x) for x in conn.execute(sql,args).fetchall()]
def password_ok(user,pw): return hashlib.sha256(pw.encode()).hexdigest()==user["password_hash"]
def cookie_value(headers,name):
    for part in headers.get("Cookie","").split(";"):
        k,_,v=part.strip().partition("=")
        if k==name: return v
    return ""
def session_user(handler): return SESSIONS.get(cookie_value(handler.headers,"sid"))

def qwen_analyze(payload):
    api_key=os.getenv("DASHSCOPE_API_KEY") or os.getenv("AI_API_KEY")
    if not api_key: raise RuntimeError("未配置千问 API Key，请在 Render 环境变量中配置 DASHSCOPE_API_KEY")
    base_url=os.getenv("AI_BASE_URL","https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model=os.getenv("AI_MODEL","qwen-plus")
    system="""你是销售管理者的经营分析助手。只能依据输入数据分析，不得编造客户、金额、日期或项目进展。请输出严格 JSON，不要 Markdown 代码块，结构为：summary（字符串）、focus（数组，每项含 direction/finding/action/evidence）、questions（字符串数组）、actions（字符串数组）、data_gaps（字符串数组）。重点回答管理者应该关注什么、为什么关注、下一步怎么管。"""
    body={"model":model,"temperature":0.2,"messages":[{"role":"system","content":system},{"role":"user","content":"请分析以下销售工作台数据：\n"+json.dumps(payload,ensure_ascii=False)}]}
    req=http_request.Request(base_url+"/chat/completions",data=json.dumps(body,ensure_ascii=False).encode(),headers={"Authorization":"Bearer "+api_key,"Content-Type":"application/json"},method="POST")
    try:
        with http_request.urlopen(req,timeout=60) as res: response=json.loads(res.read().decode())
    except http_error.HTTPError as exc:
        raise RuntimeError(f"千问接口调用失败（HTTP {exc.code}）")
    except (http_error.URLError, TimeoutError):
        raise RuntimeError("千问接口连接失败，请检查 Render 网络和 AI_BASE_URL")
    content=response.get("choices",[{}])[0].get("message",{}).get("content","")
    if isinstance(content,list): content="".join(x.get("text","") for x in content if isinstance(x,dict))
    content=content.strip().removeprefix("```json").removesuffix("```").strip()
    try: result=json.loads(content)
    except json.JSONDecodeError: raise RuntimeError("千问返回内容不是有效的结构化分析结果")
    for key,default in (("summary","暂无总结"),("focus",[]),("questions",[]),("actions",[]),("data_gaps",[])):
        result.setdefault(key,default)
    return result

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def send_json(self,obj,status=200,headers=None):
        raw=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def send_file(self,path):
        raw=path.read_bytes(); self.send_response(200); self.send_header("Content-Type",mimetypes.guess_type(str(path))[0] or "application/octet-stream"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        u=urlparse(self.path); path=u.path
        if path=="/api/me":
            u=SESSIONS.get(cookie_value(self.headers,"sid"));
            if not u: self.send_json({"error":"未登录"},401)
            else: self.send_json({"user":u})
            return
        if path=="/api/users":
            u=SESSIONS.get(cookie_value(self.headers,"sid"));
            if not u or u["role"]!="admin": self.send_json({"error":"无权限"},403); return
            with db() as c: data=rows(c,"SELECT id,username,display_name name,role,active FROM users ORDER BY id")
            self.send_json({"users":data}); return
        if path=="/api/bootstrap":
            actor=session_user(self)
            if not actor: self.send_json({"error":"未登录"},401); return
            with db() as c:
                owner_filter="" if actor["role"]=="admin" else " WHERE o.owner=?"
                args=() if actor["role"]=="admin" else (actor["name"],)
                org=rows(c,"SELECT * FROM organizations" + (" ORDER BY id DESC" if not owner_filter else " WHERE owner=? ORDER BY id DESC"), () if actor["role"]=="admin" else args)
                for o in org:
                    o.update(key=o.get("key_name", ""), phone=o.get("key_phone", ""), title=o.get("key_title", ""), other=o.get("other_contact", ""), otherPhone=o.get("other_phone", ""))
                acts=rows(c,"SELECT a.*,o.name target FROM activities a JOIN organizations o ON o.id=a.organization_id" + owner_filter + " ORDER BY event_date DESC",args)
                for a in acts:
                    a.update(date=a.get("event_date", ""), type=a.get("event_type", ""), people=a.get("participants", 0))
                for a in acts: a["followUps"]=rows(c,"SELECT follow_date date,wechat,leads,deals,note FROM followups WHERE activity_id=? ORDER BY id",(a["id"],))
                reps=rows(c,"SELECT user_name,payload,submitted FROM reports ORDER BY report_date DESC"); reports={r["user_name"]:{**json.loads(r["payload"]),"submitted":bool(r["submitted"])} for r in reps}
                people=rows(c,"SELECT display_name name FROM users WHERE role='sales' AND active=1 ORDER BY id")
                self.send_json({"associations":[x for x in org if x["kind"]=="association"],"partners":[x for x in org if x["kind"]=="partner"],"activities":acts,"reports":reports,"people":[x["name"] for x in people]}); return
        if path.startswith("/uploads/"):
            p=(UPLOADS/path.removeprefix("/uploads/")).resolve()
            if UPLOADS in p.parents and p.exists(): self.send_file(p); return
        if path=="/api/export":
            actor=session_user(self)
            if not actor: self.send_json({"error":"未登录"},401); return
            with db() as c:
                data=rows(c,"SELECT a.kind,o.name target,a.event_date,a.event_type,a.owner,a.cost,a.participants,a.wechat,a.leads,a.deals,a.photos,a.note FROM activities a JOIN organizations o ON o.id=a.organization_id"+(" ORDER BY event_date DESC" if actor["role"]=="admin" else " WHERE a.owner=? ORDER BY event_date DESC"),() if actor["role"]=="admin" else (actor["name"],))
            out=io.StringIO(); w=csv.DictWriter(out,fieldnames=data[0].keys() if data else ["kind","target"]); w.writeheader(); w.writerows(data); raw=("\ufeff"+out.getvalue()).encode(); self.send_response(200); self.send_header("Content-Type","text/csv; charset=utf-8"); self.send_header("Content-Disposition","attachment; filename=sales-workbench.csv"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        rel="index.html" if path in ("/","/index.html") else path.lstrip("/"); p=(ROOT/rel).resolve()
        if ROOT in p.parents and p.exists() and p.is_file(): self.send_file(p); return
        self.send_json({"error":"not found"},404)
    def do_POST(self):
        path=urlparse(self.path).path
        if path.startswith("/api/users/") and path.endswith("/deactivate"):
            actor=SESSIONS.get(cookie_value(self.headers,"sid"));
            if not actor or actor["role"]!="admin": self.send_json({"error":"无权限"},403); return
            uid=path.split("/")[3]
            with db() as c: c.execute("UPDATE users SET active=0 WHERE id=? AND username<>?",(uid,actor["username"]))
            self.send_json({"ok":True}); return
        if path=="/api/login":
            d=read_json(self); 
            with db() as c: u=c.execute("SELECT * FROM users WHERE username=? AND active=1",(d.get("username",""),)).fetchone()
            if not u or not password_ok(u,d.get("password","")): self.send_json({"error":"用户名或密码错误"},401); return
            user={"id":u["id"],"username":u["username"],"name":u["display_name"],"role":u["role"]}; token=secrets.token_urlsafe(32); SESSIONS[token]=user
            raw=json.dumps({"user":user},ensure_ascii=False).encode(); self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Set-Cookie",f"sid={token}; Path=/; HttpOnly; SameSite=Lax"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if path=="/api/logout":
            SESSIONS.pop(cookie_value(self.headers,"sid"),None); self.send_response(204); self.send_header("Set-Cookie","sid=; Path=/; Max-Age=0"); self.end_headers(); return
        if path=="/api/users":
            actor=SESSIONS.get(cookie_value(self.headers,"sid"));
            if not actor or actor["role"]!="admin": self.send_json({"error":"只有管理员可以创建账号"},403); return
            d=read_json(self); username=d.get("username","").strip(); name=d.get("name","").strip(); password=d.get("password",""); role=d.get("role","sales")
            if not username or not name or len(password)<6 or role not in ("admin","sales"): self.send_json({"error":"账号、姓名、密码（至少6位）和角色不能为空"},400); return
            try:
                with db() as c: cur=c.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES(?,?,?,?)",(username,name,role,hashlib.sha256(password.encode()).hexdigest()))
                self.send_json({"id":cur.lastrowid},201)
            except sqlite3.IntegrityError: self.send_json({"error":"账号已存在"},409)
            return
        if path.startswith("/api/users/") and path.endswith("/deactivate"):
            actor=SESSIONS.get(cookie_value(self.headers,"sid"));
            if not actor or actor["role"]!="admin": self.send_json({"error":"无权限"},403); return
            uid=path.split("/")[3]
            with db() as c: c.execute("UPDATE users SET active=0 WHERE id=? AND username<>?",(uid,actor["username"]))
            self.send_json({"ok":True}); return
        if path=="/api/state":
            if not session_user(self): self.send_json({"error":"未登录"},401); return
            d=read_json(self)
            with db() as c: c.execute("INSERT INTO app_state(id,payload,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",(json.dumps(d,ensure_ascii=False),now()))
            self.send_json({"ok":True}); return
        if path=="/api/organizations":
            actor=session_user(self)
            if not actor or actor["role"]!="admin": self.send_json({"error":"只有管理员可以维护基础信息"},403); return
            d=read_json(self)
            d["kind"]={"associations":"association","partners":"partner"}.get(d.get("kind"),d.get("kind"))
            try:
                with db() as c: cur=c.execute("INSERT INTO organizations(kind,name,owner,cost,key_name,key_phone,key_title,other_contact,other_phone,note) VALUES(?,?,?,?,?,?,?,?,?,?)",(d["kind"],d["name"],d["owner"],d.get("cost",0),d.get("key",""),d.get("phone",""),d.get("title",""),d.get("other",""),d.get("otherPhone",""),d.get("note","")))
                self.send_json({"id":cur.lastrowid},201)
            except sqlite3.IntegrityError: self.send_json({"error":"同类型名称已存在"},409)
            return
        if path.startswith("/api/organizations/") and path.endswith("/deactivate"):
            actor=session_user(self)
            if not actor or actor["role"]!="admin": self.send_json({"error":"只有管理员可以停用基础信息"},403); return
            oid=path.split("/")[3]
            with db() as c: c.execute("UPDATE organizations SET active=0 WHERE id=?",(oid,))
            self.send_json({"ok":True}); return
        if path.startswith("/api/organizations/"):
            actor=session_user(self)
            if not actor or actor["role"]!="admin": self.send_json({"error":"只有管理员可以修改基础信息"},403); return
            oid=path.split("/")[3]; d=read_json(self)
            try:
                with db() as c: c.execute("UPDATE organizations SET name=?,owner=?,cost=?,key_name=?,key_phone=?,key_title=?,other_contact=?,other_phone=?,note=? WHERE id=?",(d["name"],d["owner"],d.get("cost",0),d.get("key",""),d.get("phone",""),d.get("title",""),d.get("other",""),d.get("otherPhone",""),d.get("note",""),oid))
                self.send_json({"ok":True})
            except sqlite3.IntegrityError: self.send_json({"error":"同类型名称已存在"},409)
            return
        if path=="/api/activities":
            actor=session_user(self)
            if not actor: self.send_json({"error":"未登录"},401); return
            d=read_json(self)
            with db() as c:
                o=c.execute("SELECT id,owner FROM organizations WHERE kind=? AND name=? AND active=1",(d["kind"],d["target"])).fetchone()
                if not o: self.send_json({"error":"基础信息不存在或已停用"},400); return
                if actor["role"]!="admin" and o["owner"]!=actor["name"]: self.send_json({"error":"销售只能登记自己负责对象的活动"},403); return
                cur=c.execute("INSERT INTO activities(kind,organization_id,event_date,event_type,owner,cost,participants,wechat,leads,deals,photos,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(d["kind"],o["id"],d["date"],d["type"],o["owner"],d.get("cost",0),d.get("people",0),d.get("wechat",0),d.get("leads",0),d.get("deals",0),d.get("photos",""),d.get("note",""),now()))
            self.send_json({"id":cur.lastrowid},201); return
        if path.startswith("/api/activities/") and path.endswith("/followups"):
            actor=session_user(self)
            if not actor: self.send_json({"error":"未登录"},401); return
            aid=path.split("/")[3]; d=read_json(self)
            with db() as c:
                activity=c.execute("SELECT owner FROM activities WHERE id=?",(aid,)).fetchone()
                if not activity or (actor["role"]!="admin" and activity["owner"]!=actor["name"]): self.send_json({"error":"无权修改其他销售的跟进记录"},403); return
                c.execute("INSERT INTO followups(activity_id,follow_date,wechat,leads,deals,note) VALUES(?,?,?,?,?,?)",(aid,d.get("date",now()[:10]),d.get("wechat",0),d.get("leads",0),d.get("deals",0),d.get("note","")))
                c.execute("UPDATE activities SET wechat=wechat+?,leads=leads+?,deals=deals+? WHERE id=?",(d.get("wechat",0),d.get("leads",0),d.get("deals",0),aid))
            self.send_json({"ok":True}); return
        if path=="/api/reports":
            actor=session_user(self)
            if not actor: self.send_json({"error":"未登录"},401); return
            d=read_json(self); user=d.get("user") if actor["role"]=="admin" else actor["name"]; date=d.get("date",time.strftime("%Y-%m-%d")); payload=json.dumps(d.get("payload",{}),ensure_ascii=False)
            with db() as c: c.execute("INSERT INTO reports(user_name,report_date,payload,submitted,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_name,report_date) DO UPDATE SET payload=excluded.payload,submitted=excluded.submitted,updated_at=excluded.updated_at",(user,date,payload,int(bool(d.get("submitted"))),now()))
            self.send_json({"ok":True}); return
        if path=="/api/ai/analyze":
            actor=session_user(self)
            if not actor or actor["role"]!="admin": self.send_json({"error":"只有管理员可以生成 AI 分析"},403); return
            d=read_json(self); target=d.get("target","team")
            with db() as c:
                act=rows(c,"SELECT a.event_date date,a.event_type type,a.owner,a.cost,a.participants people,a.wechat,a.leads,a.deals,a.note,o.name target FROM activities a JOIN organizations o ON o.id=a.organization_id"+(" WHERE a.owner=?" if target!="team" else ""), (target,) if target!="team" else ())
                if target=="team": rep_rows=rows(c,"SELECT user_name,report_date,payload FROM reports WHERE submitted=1 ORDER BY report_date DESC")
                else: rep_rows=rows(c,"SELECT user_name,report_date,payload FROM reports WHERE submitted=1 AND user_name=? ORDER BY report_date DESC",(target,))
            reports=[]
            for r in rep_rows:
                try: reports.append({"sales":r["user_name"],"date":r["report_date"],"content":json.loads(r["payload"])})
                except (TypeError,ValueError,json.JSONDecodeError): pass
            context={"target":"团队" if target=="team" else target,"reports":reports,"activities":act}
            try: result=qwen_analyze(context)
            except RuntimeError as exc: self.send_json({"error":str(exc)},502); return
            result["target"]=target; result["generated_at"]=now()
            with db() as c: c.execute("INSERT INTO ai_analyses(target_key,report_date,payload,updated_at) VALUES(?,?,?,?) ON CONFLICT(target_key,report_date) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",(target,time.strftime("%Y-%m-%d"),json.dumps(result,ensure_ascii=False),now()))
            self.send_json(result); return
        if path=="/api/ai/save":
            actor=session_user(self)
            if not actor or actor["role"]!="admin": self.send_json({"error":"只有管理员可以保存 AI 分析"},403); return
            d=read_json(self); target=d.get("target","team"); note=d.get("note","")
            with db() as c: c.execute("UPDATE ai_analyses SET manager_note=?,updated_at=? WHERE target_key=? AND report_date=?",(note,now(),target,time.strftime("%Y-%m-%d")))
            self.send_json({"ok":True}); return
        self.send_json({"error":"not found"},404)

    def do_PUT(self):
        return self.do_POST()

if __name__=="__main__":
    init(); print(f"销售工作台运行中：http://{HOST}:{PORT}"); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
