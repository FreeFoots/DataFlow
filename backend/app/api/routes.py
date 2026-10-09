from __future__ import annotations

import sqlite3
import time
import uuid
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import Field

from ..errors import PipelineStageError
from ..models import (
    ClarificationRequest,
    LoginRequest,
    QueryRequest,
    QueryResult,
    SaveFieldMemoryRequest,
    SaveMemoryRequest,
    SchemaTable,
)
from ..security import AuthService, AuthUser
from ..services.dataflow_service import DataFlowService
from ..runtime.manager import TaskManager
from ..runtime.store import ConflictError
from ..config import settings


router = APIRouter(prefix="/api")
service = DataFlowService()
task_manager = TaskManager(service, settings)
auth_service = AuthService(task_manager.store)

def require_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> AuthUser:
    user = auth_service.authenticate(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


class TaskSubmission(QueryRequest):
    submission_key: str = Field(min_length=8, max_length=128)


class DurableClarification(ClarificationRequest):
    submission_key: str = Field(min_length=8, max_length=128)
    version: int = Field(ge=1)


def task_error(error):
    if isinstance(error, PermissionError):
        raise HTTPException(403, str(error)) from error
    if isinstance(error, KeyError):
        raise HTTPException(404, "任务不存在") from error
    if isinstance(error, ConflictError):
        raise HTTPException(409, str(error)) from error
    if isinstance(error, ValueError):
        raise HTTPException(400, str(error)) from error
    if isinstance(error, (sqlite3.Error, OSError)):
        raise HTTPException(503, "任务存储暂时不可用，请稍后重试") from error
    raise error


@router.post("/tasks", status_code=202)
def submit_task(payload: TaskSubmission, user: AuthUser = Depends(require_user)):
    request = payload.model_dump(exclude={"submission_key"})
    request["query"] = request["query"].strip()
    if not request["query"]:
        raise HTTPException(400, "问题不能为空")
    try:
        return task_manager.submit(user.user_id, request, payload.submission_key)
    except (ConflictError, sqlite3.Error, OSError) as error:
        task_error(error)


@router.get("/task-history")
def task_history(user: AuthUser = Depends(require_user)):
    try:
        return task_manager.history(user.user_id)
    except (sqlite3.Error, OSError) as error:
        task_error(error)


@router.get("/ready")
def readiness():
    try:
        with task_manager.store.connection() as db:
            db.execute("SELECT 1 FROM runtime_tasks LIMIT 1")
        if not task_manager.worker.thread or not task_manager.worker.thread.is_alive():
            raise HTTPException(503, "后台处理器暂时不可用")
        return {"status": "ready", "durable_tasks": True}
    except (sqlite3.Error, OSError) as error:
        task_error(error)


@router.get("/tasks/{task_id}/state")
def task_state(task_id: str, user: AuthUser = Depends(require_user)):
    try:
        return task_manager.get(task_id, user.user_id)
    except (KeyError, PermissionError, sqlite3.Error) as error:
        task_error(error)


@router.get("/tasks/{task_id}/events")
def task_events(task_id: str, after: int = 0, user: AuthUser = Depends(require_user)):
    try:
        return task_manager.store.events(task_id, user.user_id, after)
    except (KeyError, PermissionError, sqlite3.Error) as error:
        task_error(error)


@router.post("/tasks/{task_id}/resume", status_code=202)
def resume_task(task_id: str, payload: DurableClarification, user: AuthUser = Depends(require_user)):
    try:
        return task_manager.public(task_manager.store.clarify(task_id, user.user_id, payload.option_id, payload.submission_key, payload.version, payload.answer))
    except (KeyError, PermissionError, ValueError, sqlite3.Error) as error:
        task_error(error)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, user: AuthUser = Depends(require_user)):
    try:
        return task_manager.public(task_manager.store.cancel(task_id, user.user_id))
    except (KeyError, PermissionError, sqlite3.Error) as error:
        task_error(error)


@router.post("/sessions/{session_id}/hide")
def hide_session(session_id: str, user: AuthUser = Depends(require_user)):
    try:
        task_manager.store.hide_session(user.user_id, session_id)
        return {"hidden": True}
    except (ConflictError, sqlite3.Error) as error:
        task_error(error)



def require_admin(user: AuthUser = Depends(require_user)) -> AuthUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以执行该操作")
    return user


@router.get("/health")
def health() -> dict:
    return {"status": "ok", **service.status()}


@router.post("/auth/login")
def login(payload: LoginRequest) -> dict:
    result = auth_service.login(payload.username, payload.password)
    if not result:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    token, user = result
    return {"access_token": token, "token_type": "bearer", "user": user.public()}


@router.get("/auth/me")
def me(user: AuthUser = Depends(require_user)) -> dict[str, str]:
    return user.public()


@router.post("/auth/logout")
def logout(
    authorization: str | None = Header(default=None, alias="Authorization"),
    _: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    auth_service.logout(authorization)
    return {"logged_out": True}


@router.get("/schema", response_model=list[SchemaTable])
def schema(
    user: AuthUser = Depends(require_user),
) -> list[dict]:
    return service.visible_schema(user.user_id)


@router.get("/schema/search")
def search_schema(
    q: str,
    threshold: float | None = None,
    user: AuthUser = Depends(require_user),
) -> dict:
    if not q.strip():
        raise HTTPException(status_code=400, detail="检索问题不能为空")
    if threshold is not None and not 0 <= threshold <= 1:
        raise HTTPException(status_code=400, detail="阈值必须在0到1之间")
    try:
        return service.search_schema(q.strip(), threshold, user.user_id)
    except PipelineStageError as error:
        raise HTTPException(
            status_code=502,
            detail=f"{error.stage}失败：{error.message}",
        ) from error


@router.post("/schema/index/rebuild")
def rebuild_schema_index(_: AuthUser = Depends(require_admin)) -> dict:
    try:
        return service.rebuild_schema_index()
    except PipelineStageError as error:
        raise HTTPException(
            status_code=502,
            detail=f"{error.stage}失败：{error.message}",
        ) from error


@router.get("/config")
def config(_: AuthUser = Depends(require_user)) -> dict:
    return service.status()


@router.get("/mcp/tools")
def mcp_tools(
    user: AuthUser = Depends(require_user),
) -> list[dict]:
    """查看模型可用的MCP工具及其输入输出Schema。"""
    return service.list_mcp_tools(user.user_id)


@router.get("/skills")
def skills(_: AuthUser = Depends(require_user)) -> list[dict]:
    """查看当前启用的应用级Skill。"""
    return service.list_skills()


@router.post("/query", response_model=QueryResult)
def query(
    payload: QueryRequest,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    # Compatibility clients still wait for a result, but execution no longer belongs to HTTP.
    try:
        task = task_manager.submit(user.user_id, payload.model_dump(), uuid.uuid4().hex)
        return wait_for_result(task["task_id"], user.user_id)
    except (ConflictError, sqlite3.Error, OSError) as error:
        task_error(error)


def wait_for_result(task_id: str, user_id: str) -> QueryResult:
    deadline = time.monotonic() + settings.task_timeout_seconds + 15
    while time.monotonic() < deadline:
        task = task_manager.get(task_id, user_id)
        if task["result"]:
            return QueryResult.model_validate(task["result"])
        time.sleep(.2)
    raise HTTPException(504, f"任务已保存，请使用任务编号继续查看：{task_id}")


@router.post("/tasks/{task_id}/clarify", response_model=QueryResult)
def clarify(
    task_id: str,
    payload: ClarificationRequest,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    try:
        try:
            task = task_manager.store.get(task_id, user.user_id)
        except KeyError:
            task = None
        if task:
            task_manager.store.clarify(task_id, user.user_id, payload.option_id, uuid.uuid4().hex, task["version"], payload.answer)
            return wait_for_result(task_id, user.user_id)
        if payload.answer:
            raise ValueError("该旧任务不支持自由补充，请重新提交原问题")
        return service.clarify(task_id, payload.option_id, user_id=user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="查询任务不存在") from error
    except ValueError as error:
        task_error(error)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (sqlite3.Error, OSError) as error:
        task_error(error)


@router.get("/tasks/{task_id}", response_model=QueryResult)
def task(
    task_id: str,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    try:
        try:
            durable = task_manager.get(task_id, user.user_id)
        except KeyError:
            durable = None
        if durable:
            if not durable["result"]:
                raise HTTPException(409, "任务尚在处理，请查询任务状态接口")
            return QueryResult.model_validate(durable["result"])
        return service.get_task(task_id, user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="查询任务不存在") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.post("/memories")
def save_memory(
    payload: SaveMemoryRequest,
    user: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    try:
        try:
            task_manager.get(payload.task_id, user.user_id)
        except KeyError:
            pass  # Existing synchronous tasks retain their compatibility path.
        service.save_memory(payload.task_id, user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="没有可保存的查询结果") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"saved": True}


@router.get("/memories")
def memories(user: AuthUser = Depends(require_user)) -> list[dict]:
    return service.list_memories(user.user_id)


@router.post("/memories/fields")
def save_field_memory(
    payload: SaveFieldMemoryRequest,
    user: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    try:
        service.save_field_memory(
            payload.table_id,
            payload.name,
            payload.label,
            payload.field_type,
            user.user_id,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"saved": True}


@router.delete("/memories/{memory_id}")
def delete_memory(
    memory_id: str,
    user: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    service.delete_memory(memory_id, user.user_id)
    return {"deleted": True}
