from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, func
from sqlalchemy.orm import sessionmaker, Session, declarative_base
import hashlib
import secrets
import re
from typing import Optional
from datetime import datetime

# --- НАСТРОЙКА БАЗЫ ДАННЫХ ---
engine = create_engine("sqlite:///./pro_kanban.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- МОДЕЛИ ДАННЫХ (Для БД) ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)
    token = Column(String, unique=True, index=True)
    last_ws_id = Column(Integer, nullable=True)

class Workspace(Base):
    __tablename__ = "workspaces"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    owner_id = Column(Integer, ForeignKey("users.id"))

class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    user_id = Column(Integer, ForeignKey("users.id"))

class Stage(Base):
    __tablename__ = "stages"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    order_index = Column(Integer, default=0)

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    stage_id = Column(Integer, ForeignKey("stages.id"), nullable=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"))
    start_date = Column(String, nullable=True)
    end_date = Column(String, nullable=True)
    urgency = Column(Float, default=50.0)
    importance = Column(Float, default=50.0)

class TaskLog(Base):
    __tablename__ = "task_logs"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"))
    username = Column(String)
    action = Column(String)
    timestamp = Column(String)

Base.metadata.create_all(bind=engine)

# --- DTO СХЕМЫ ---
class AuthReq(BaseModel): username: str; password: str
class PassChangeReq(BaseModel): old_password: str; new_password: str
class WorkspaceReq(BaseModel): name: str
class StageReq(BaseModel): name: str; workspace_id: int
class StageMoveReq(BaseModel): direction: int
class TaskReq(BaseModel): title: str; stage_id: Optional[int]; workspace_id: int; start_date: Optional[str]; end_date: Optional[str]
class TaskUpdate(BaseModel): title: Optional[str]=None; stage_id: Optional[int]=None; urgency: Optional[float]=None; importance: Optional[float]=None; start_date: Optional[str]=None; end_date: Optional[str]=None

# --- БИЗНЕС-ЛОГИКА (СЛОЙ СЕРВИСОВ - ООП) ---

class AuthService:
    def __init__(self, db: Session):
        self.db = db

    def change_password(self, user: User, data: PassChangeReq):
        if user.password_hash != self._hash_pass(data.old_password):
            raise HTTPException(400, "Старый пароль неверный")
        if len(data.new_password) < 6:
            raise HTTPException(400, "Короткий новый пароль")

        user.password_hash = self._hash_pass(data.new_password)
        self.db.commit()
        return {"msg": "Пароль успешно изменен"}

    def _hash_pass(self, password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def register(self, data: AuthReq) -> dict:
        if not re.match(r"^[A-Za-z0-9_]+$", data.username): raise HTTPException(400, "Недопустимый логин")
        if len(data.password) < 6: raise HTTPException(400, "Короткий пароль")
        if self.db.query(User).filter(User.username == data.username).first(): raise HTTPException(400, "Пользователь существует")
        user = User(username=data.username, password_hash=self._hash_pass(data.password), token=secrets.token_hex(16))
        self.db.add(user); self.db.commit()
        return {"token": user.token, "username": user.username}

    def login(self, data: AuthReq) -> dict:
        user = self.db.query(User).filter(User.username == data.username, User.password_hash == self._hash_pass(data.password)).first()
        if not user: raise HTTPException(400, "Неверные данные")
        return {"token": user.token, "username": user.username}

    def update_last_ws(self, user: User, ws_id: int):
        user.last_ws_id = ws_id
        self.db.commit()


class WorkspaceService:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, user: User) -> list:
        member_ws_ids = [m.workspace_id for m in self.db.query(WorkspaceMember).filter(WorkspaceMember.user_id == user.id).all()]
        return self.db.query(Workspace).filter((Workspace.owner_id == user.id) | (Workspace.id.in_(member_ws_ids))).all()

    def create(self, data: WorkspaceReq, user: User) -> Workspace:
        ws = Workspace(name=data.name, owner_id=user.id)
        self.db.add(ws); self.db.commit(); self.db.refresh(ws)
        self.db.add_all([
            Stage(name="Бэклог", workspace_id=ws.id, order_index=1),
            Stage(name="В работе", workspace_id=ws.id, order_index=2),
            Stage(name="Готово", workspace_id=ws.id, order_index=3)
        ])
        user.last_ws_id = ws.id
        self.db.commit()
        return ws


class TaskService:
    def __init__(self, db: Session):
        self.db = db

    def _validate_dates(self, start, end):
        if start and end:
            try:
                if datetime.strptime(start, "%Y-%m-%d") > datetime.strptime(end, "%Y-%m-%d"):
                    raise HTTPException(400, "Дата начала позже окончания")
            except ValueError: pass

    def _write_log(self, task_id: int, username: str, action: str):
        self.db.add(TaskLog(task_id=task_id, username=username, action=action,
                            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self.db.commit()  # <--- ДОБАВЬ ЭТУ СТРОЧКУ!
    def create_task(self, data: TaskReq, user: User) -> Task:
        self._validate_dates(data.start_date, data.end_date)
        t = Task(**data.model_dump())
        self.db.add(t); self.db.commit(); self.db.refresh(t)
        self._write_log(t.id, user.username, "Создал(а) задачу")
        return t

    def update_task(self, task_id: int, data: TaskUpdate, user: User) -> Task:
        t = self.db.query(Task).filter(Task.id == task_id).first()
        if not t: raise HTTPException(404, "Не найдено")

        self._validate_dates(data.start_date or t.start_date, data.end_date or t.end_date)

        # Собираем умный лог изменений
        log_actions = []

        if data.title is not None and data.title != t.title:
            log_actions.append(f"Изменил(а) название на \"{data.title}\" (было: \"{t.title}\")")

        if data.stage_id is not None and data.stage_id != t.stage_id:
            old_st = self.db.query(Stage).filter(Stage.id == t.stage_id).first() if t.stage_id else None
            new_st = self.db.query(Stage).filter(Stage.id == data.stage_id).first() if data.stage_id else None
            old_name = f'"{old_st.name}"' if old_st else "Без колонки"
            new_name = f'"{new_st.name}"' if new_st else "Без колонки"
            log_actions.append(f"Переместил(а) задачу: {old_name} → {new_name}")

        if data.start_date is not None and data.start_date != t.start_date:
            log_actions.append(f"Дата начала: {t.start_date or 'Не указана'} → {data.start_date or 'Сброшена'}")

        if data.end_date is not None and data.end_date != t.end_date:
            log_actions.append(f"Дата завершения: {t.end_date or 'Не указана'} → {data.end_date or 'Сброшена'}")

        if data.urgency is not None and data.urgency != t.urgency:
            log_actions.append(f"Срочность: {t.urgency:.0f}% → {data.urgency:.0f}%")

        if data.importance is not None and data.importance != t.importance:
            log_actions.append(f"Важность: {t.importance:.0f}% → {data.importance:.0f}%")

        # Применяем изменения
        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(t, key, value)

        self.db.commit()

        # Записываем каждое изменение как отдельный лог
        for action in log_actions:
            self._write_log(t.id, user.username, action)

        return t

# --- ИНИЦИАЛИЗАЦИЯ И РОУТЕРЫ FastAPI ---

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="templates")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def get_user(x_token: str = Header(None), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.token == x_token).first()
    if not user: raise HTTPException(401, "Неверный токен")
    return user

@app.get("/", response_class=HTMLResponse)
async def serve_frontend(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

# Использование сервисов ООП в роутах
@app.post("/auth/register")
def register(data: AuthReq, db: Session = Depends(get_db)):
    return AuthService(db).register(data)

@app.post("/auth/login")
def login(data: AuthReq, db: Session = Depends(get_db)):
    return AuthService(db).login(data)

@app.get("/auth/me")
def get_me(user: User = Depends(get_user)):
    return {"username": user.username, "last_ws_id": user.last_ws_id}

@app.put("/auth/last_workspace/{ws_id}")
def update_last_workspace(ws_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    AuthService(db).update_last_ws(user, ws_id)
    return {"msg": "Сохранено"}

@app.get("/workspaces")
def get_workspaces(user: User = Depends(get_user), db: Session = Depends(get_db)):
    return WorkspaceService(db).get_all(user)

@app.post("/workspaces")
def create_workspace(data: WorkspaceReq, user: User = Depends(get_user), db: Session = Depends(get_db)):
    return WorkspaceService(db).create(data, user)

@app.post("/tasks")
def create_task(data: TaskReq, user: User = Depends(get_user), db: Session = Depends(get_db)):
    return TaskService(db).create_task(data, user)

@app.put("/tasks/{task_id}")
def update_task(task_id: int, data: TaskUpdate, user: User = Depends(get_user), db: Session = Depends(get_db)):
    return TaskService(db).update_task(task_id, data, user)

@app.get("/tasks/{ws_id}")
def get_tasks(ws_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.workspace_id == ws_id).all()
    for t in tasks: t.score = (t.importance * 1.5) + (t.urgency * 1.0)
    return tasks

@app.get("/stages/{ws_id}")
def get_stages(ws_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    return db.query(Stage).filter(Stage.workspace_id == ws_id).order_by(Stage.order_index).all()

@app.get("/tasks/{task_id}/logs")
def get_task_logs(task_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task: raise HTTPException(404, "Задача не найдена")
    return db.query(TaskLog).filter(TaskLog.task_id == task_id).order_by(TaskLog.id.desc()).all()

# === ВОЗВРАЩАЕМ ПОТЕРЯННЫЕ ЭНДПОИНТЫ ===

# --- УПРАВЛЕНИЕ КОМАНДОЙ И ПРОЕКТАМИ ---
@app.get("/workspaces/{ws_id}/members")
def get_members(ws_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    ws = db.query(Workspace).filter(Workspace.id == ws_id).first()
    if not ws: raise HTTPException(404, "Проект не найден")
    result = []
    owner = db.query(User).filter(User.id == ws.owner_id).first()
    if owner: result.append({"id": owner.id, "username": owner.username, "role": "Владелец"})
    members = db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws_id).all()
    for m in members:
        u = db.query(User).filter(User.id == m.user_id).first()
        if u and u.id != ws.owner_id: result.append({"id": u.id, "username": u.username, "role": "Участник"})
    return result


@app.post("/workspaces/{ws_id}/members/{username}")
def add_member(ws_id: int, username: str, user: User = Depends(get_user), db: Session = Depends(get_db)):
    ws = db.query(Workspace).filter(Workspace.id == ws_id).first()
    if not ws:
        raise HTTPException(404, "Проект не найден")

    # Явная проверка на владельца
    if ws.owner_id != user.id:
        raise HTTPException(403, "Только владелец")

    target_user = db.query(User).filter(User.username == username).first()
    if not target_user:
        raise HTTPException(404, "Пользователь не найден")

    if target_user.id == user.id:
        raise HTTPException(400, "Вы и так владелец")

    if db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws_id,
                                        WorkspaceMember.user_id == target_user.id).first():
        raise HTTPException(400, "Уже в проекте")

    db.add(WorkspaceMember(workspace_id=ws_id, user_id=target_user.id))
    db.commit()
    return {"msg": "Добавлен"}

@app.delete("/workspaces/{ws_id}/members/{target_username}")
def remove_member(ws_id: int, target_username: str, user: User = Depends(get_user), db: Session = Depends(get_db)):
    ws = db.query(Workspace).filter(Workspace.id == ws_id, Workspace.owner_id == user.id).first()
    if not ws: raise HTTPException(403, "Только владелец")
    target_user = db.query(User).filter(User.username == target_username).first()
    if not target_user: raise HTTPException(404, "Пользователь не найден")
    db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws_id, WorkspaceMember.user_id == target_user.id).delete()
    db.commit()
    return {"msg": "Удален"}

@app.delete("/workspaces/{ws_id}")
def delete_workspace(ws_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    ws = db.query(Workspace).filter(Workspace.id == ws_id, Workspace.owner_id == user.id).first()
    if not ws: raise HTTPException(403, "Только владелец")
    tasks = db.query(Task).filter(Task.workspace_id == ws_id).all()
    for t in tasks: db.query(TaskLog).filter(TaskLog.task_id == t.id).delete()
    db.query(Task).filter(Task.workspace_id == ws_id).delete()
    db.query(Stage).filter(Stage.workspace_id == ws_id).delete()
    db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws_id).delete()
    db.delete(ws)
    if user.last_ws_id == ws_id: user.last_ws_id = None
    db.commit()
    return {"msg": "Удален"}

# --- УПРАВЛЕНИЕ ЭТАПАМИ (КОЛОНКАМИ) И ЗАДАЧАМИ ---
@app.post("/stages")
def create_stage(data: StageReq, user: User = Depends(get_user), db: Session = Depends(get_db)):
    max_order = db.query(func.max(Stage.order_index)).filter(Stage.workspace_id == data.workspace_id).scalar() or 0
    st = Stage(name=data.name, workspace_id=data.workspace_id, order_index=max_order + 1)
    db.add(st); db.commit()
    return st

@app.put("/stages/{stage_id}/move")
def move_stage(stage_id: int, data: StageMoveReq, user: User = Depends(get_user), db: Session = Depends(get_db)):
    stage = db.query(Stage).filter(Stage.id == stage_id).first()
    if not stage: raise HTTPException(404, "Этап не найден")
    stages = db.query(Stage).filter(Stage.workspace_id == stage.workspace_id).order_by(Stage.order_index).all()
    idx = next((i for i, s in enumerate(stages) if s.id == stage_id), -1)
    if idx != -1 and 0 <= idx + data.direction < len(stages):
        adjacent = stages[idx + data.direction]
        stage.order_index, adjacent.order_index = adjacent.order_index, stage.order_index
        db.commit()
    return {"msg": "Перемещено"}

@app.delete("/stages/{stage_id}")
def delete_stage(stage_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    stage = db.query(Stage).filter(Stage.id == stage_id).first()
    if stage:
        db.query(Task).filter(Task.stage_id == stage_id).update({"stage_id": None})
        db.delete(stage); db.commit()
    return {"msg": "Удален"}

@app.delete("/tasks/{task_id}")
def delete_task(task_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    t = db.query(Task).filter(Task.id == task_id).first()
    if t:
        db.query(TaskLog).filter(TaskLog.task_id == task_id).delete()
        db.delete(t); db.commit()
    return {"msg": "Удален"}

@app.put("/auth/password")
def change_password(data: PassChangeReq, user: User = Depends(get_user), db: Session = Depends(get_db)):
    return AuthService(db).change_password(user, data)