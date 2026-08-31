#!/usr/bin/env python3
"""销售工作台的轻量正式版服务端：SQLite 持久化、文件上传、查询、导出和分析接口。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import csv, hashlib, io, json, mimetypes, os, secrets, sqlite3, time

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
            with db() as c:
                saved=c.execute("SELECT payload FROM app_state WHERE id=1").fetchone()
                if saved:
                    self.send_json(json.loads(saved["payload"])); return
                org=rows(c,"SELECT * FROM organizations ORDER BY id DESC"); acts=rows(c,"SELECT a.*,o.name target FROM activities a JOIN organizations o ON o.id=a.organization_id ORDER BY event_date DESC");
                for a in acts: a["followUps"]=rows(c,"SELECT follow_date date,wechat,leads,deals,note FROM followups WHERE activity_id=? ORDER BY id",(a["id"],))
                reps=rows(c,"SELECT user_name,payload,submitted FROM reports ORDER BY report_date DESC"); reports={r["user_name"]:{**json.loads(r["payload"]),"submitted":bool(r["submitted"])} for r in reps}
                people=rows(c,"SELECT display_name name FROM users WHERE role='sales' AND active=1 ORDER BY id")
                self.send_json({"associations":[x for x in org if x["kind"]=="association"],"partners":[x for x in org if x["kind"]=="partner"],"activities":acts,"reports":reports,"people":[x["name"] for x in people]}); return
        if path.startswith("/uploads/"):
            p=(UPLOADS/path.removeprefix("/uploads/")).resolve()
            if UPLOADS in p.parents and p.exists(): self.send_file(p); return
        if path=="/api/export":
            with db() as c: data=rows(c,"SELECT a.kind,o.name target,a.event_date,a.event_type,a.owner,a.cost,a.participants,a.wechat,a.leads,a.deals,a.photos,a.note FROM activities a JOIN organizations o ON o.id=a.organization_id ORDER BY event_date DESC")
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
            try:
                with db() as c: cur=c.execute("INSERT INTO organizations(kind,name,owner,cost,key_name,key_phone,key_title,other_contact,other_phone,note) VALUES(?,?,?,?,?,?,?,?,?,?)",(d["kind"],d["name"],d["owner"],d.get("cost",0),d.get("key",""),d.get("phone",""),d.get("title",""),d.get("other",""),d.get("otherPhone",""),d.get("note","")))
                self.send_json({"id":cur.lastrowid},201)
            except sqlite3.IntegrityError: self.send_json({"error":"同类型名称已存在"},409)
            return
        if path=="/api/activities":
            if not session_user(self): self.send_json({"error":"未登录"},401); return
            d=read_json(self)
            with db() as c:
                o=c.execute("SELECT id,owner FROM organizations WHERE kind=? AND name=? AND active=1",(d["kind"],d["target"])).fetchone()
                if not o: self.send_json({"error":"基础信息不存在或已停用"},400); return
                cur=c.execute("INSERT INTO activities(kind,organization_id,event_date,event_type,owner,cost,participants,wechat,leads,deals,photos,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(d["kind"],o["id"],d["date"],d["type"],o["owner"],d.get("cost",0),d.get("people",0),d.get("wechat",0),d.get("leads",0),d.get("deals",0),d.get("photos",""),d.get("note",""),now()))
            self.send_json({"id":cur.lastrowid},201); return
        if path.startswith("/api/activities/") and path.endswith("/followups"):
            if not session_user(self): self.send_json({"error":"未登录"},401); return
            aid=path.split("/")[3]; d=read_json(self)
            with db() as c:
                c.execute("INSERT INTO followups(activity_id,follow_date,wechat,leads,deals,note) VALUES(?,?,?,?,?,?)",(aid,d.get("date",now()[:10]),d.get("wechat",0),d.get("leads",0),d.get("deals",0),d.get("note","")))
                c.execute("UPDATE activities SET wechat=wechat+?,leads=leads+?,deals=deals+? WHERE id=?",(d.get("wechat",0),d.get("leads",0),d.get("deals",0),aid))
            self.send_json({"ok":True}); return
        if path=="/api/reports":
            if not session_user(self): self.send_json({"error":"未登录"},401); return
            d=read_json(self); user=d["user"]; date=d.get("date",time.strftime("%Y-%m-%d")); payload=json.dumps(d.get("payload",{}),ensure_ascii=False)
            with db() as c: c.execute("INSERT INTO reports(user_name,report_date,payload,submitted,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_name,report_date) DO UPDATE SET payload=excluded.payload,submitted=excluded.submitted,updated_at=excluded.updated_at",(user,date,payload,int(bool(d.get("submitted"))),now()))
            self.send_json({"ok":True}); return
        if path=="/api/ai/analyze":
            actor=session_user(self)
            if not actor or actor["role"]!="admin": self.send_json({"error":"只有管理员可以生成 AI 分析"},403); return
            d=read_json(self); target=d.get("target","team")
            with db() as c:
                act=rows(c,"SELECT * FROM activities WHERE owner=?",(target,)) if target!="team" else rows(c,"SELECT * FROM activities")
                rep=c.execute("SELECT COUNT(*) n FROM reports WHERE submitted=1").fetchone()["n"]
            leads=sum(float(x["leads"] or 0) for x in act); deals=sum(float(x["deals"] or 0) for x in act); result={"target":target,"summary":f"{'团队' if target=='team' else target}登记活动 {len(act)} 次，产生线索 {int(leads)}，成交金额 ¥{deals:,.0f}。已提交周报 {rep} 份。","focus":[{"direction":"线索转化","finding":"有线索但暂无成交" if leads and not deals else "请持续更新客户阶段","action":"逐条补充决策人、下一步动作和完成日期。"},{"direction":"回款节点","finding":"成交项目需要确认回款","action":"下次周会前补充客户确认的回款日期和金额。"}],"questions":["这个项目下一步具体做什么、谁负责、何时完成？","当前阻塞点是什么，需要管理者提供什么支持？"],"actions":["下次周会前完成重点项目节点补充。","对有线索未成交活动建立逐条跟进记录。"]}
            with db() as c: c.execute("INSERT INTO ai_analyses(target_key,report_date,payload,updated_at) VALUES(?,?,?,?) ON CONFLICT(target_key,report_date) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",(target,time.strftime("%Y-%m-%d"),json.dumps(result,ensure_ascii=False),now()))
            self.send_json(result); return
        self.send_json({"error":"not found"},404)

if __name__=="__main__":
    init(); print(f"销售工作台运行中：http://{HOST}:{PORT}"); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
