# tg-relay - 用 MTProto 协议号把频道消息转发到多个群
# Copyright (C) 2026 tg-relay contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""网页面板：FastAPI 应用，**跑在转发进程内部**。

⚠️ 这是整个设计的核心约束：
这个进程是唯一持有 Telegram session 的地方。网页绝不能自己新建 TelegramClient
（哪怕是"只查一下状态"），否则两个 client 共用一个 auth key
会让 Telegram 作废 auth key、账号被强制登出（AuthKeyDuplicatedError）。

所以：网页 -> RuntimeControl -> 引擎（同进程直接调用），没有 IPC、没有第二个连接。

启动：
    python -m tgrelay --web --web-port 8000
默认只绑 127.0.0.1，配合 SSH 隧道访问，不要直接暴露公网。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .control import ControlError, RuntimeControl

log = logging.getLogger("tgrelay.web")

SESSION_COOKIE = "tgrelay_token"
LOG_BUFFER_SIZE = 500


class LogBuffer(logging.Handler):
    """把日志尾部缓存在内存里，供网页实时查看。

    只收集本项目的 logger（tgrelay.*），避免被第三方库刷屏。
    """

    def __init__(self, capacity: int = LOG_BUFFER_SIZE) -> None:
        super().__init__()
        self.buffer: Deque[str] = deque(maxlen=capacity)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s", datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.name.startswith("tgrelay"):
                return
            self.buffer.append(self.format(record))
        except Exception:  # pragma: no cover - 日志处理器绝不该抛异常
            pass

    def tail(self, limit: int = 100) -> list[str]:
        return list(self.buffer)[-limit:]


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------


class TargetIn(BaseModel):
    peer: str = Field(..., description="群用户名或 -100 开头的数字 ID")
    label: str = ""
    verify: bool = True


class TargetPatch(BaseModel):
    label: str | None = None
    interval: list[float] | None = None
    enabled: bool | None = None
    daily_limit: int | None = None


class MaterialsIn(BaseModel):
    ids: list[int]
    apply: bool = True


class RepostPatch(BaseModel):
    enabled: bool | None = None
    interval: float | None = None
    daily_limit: int | None = None
    shuffle: bool | None = None


class RatePatch(BaseModel):
    global_per_minute: int | None = None
    daily_cap: int | None = None


class SourceIn(BaseModel):
    peer: str = Field(..., description="频道用户名或 -100 开头的数字 ID")
    verify: bool = True


class BulkIn(BaseModel):
    """批量操作：多群管理的核心入口。

    action:
      pause / resume          暂停或恢复（keys 为空 = 全部）
      set_limit               给选中的群设置各自的每日额度
      share_limit             把总额度平均分给选中的群
      set_interval            统一改发送间隔
    """

    action: str
    keys: list[str] | None = None
    daily_limit: int | None = None
    interval: list[float] | None = None


# --------------------------------------------------------------------------
# 应用工厂
# --------------------------------------------------------------------------


def create_app(
    control: RuntimeControl,
    *,
    token: str | None = None,
    log_buffer: LogBuffer | None = None,
    page: str | None = None,
) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    log_buffer = log_buffer or LogBuffer()
    page_html = page if page is not None else _default_page(token)

    app = FastAPI(
        title="tg-relay 控制面板",
        description="协议号多群转发 / 定时重发的运行期控制面板",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.token = token
    app.state.control = control
    app.state.log_buffer = log_buffer

    # ---------------- 鉴权 ----------------

    def _token_from(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        header = request.headers.get("x-token")
        if header:
            return header.strip()
        return request.cookies.get(SESSION_COOKIE)

    async def require_token(request: Request) -> None:
        provided = _token_from(request)
        if not provided or not secrets.compare_digest(provided, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="需要令牌。请用 /?token=... 访问一次以获得 Cookie，或带 Authorization: Bearer <token>",
            )

    def guard(exc: Exception) -> HTTPException:
        if isinstance(exc, ControlError):
            return HTTPException(status_code=400, detail=str(exc))
        log.exception("网页面板操作失败")
        return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")

    # ---------------- 页面 ----------------

    def _is_https(request: Request) -> bool:
        """判断请求是否走的 HTTPS（反代后面要读 X-Forwarded-Proto）。"""
        forwarded = request.headers.get("x-forwarded-proto", "")
        if forwarded:
            return forwarded.split(",")[0].strip().lower() == "https"
        return request.url.scheme == "https"

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """带上 ?token=xxx 访问一次，就把令牌写进 Cookie，之后不用再带。"""
        provided = request.query_params.get("token")
        response = HTMLResponse(page_html)
        if provided and secrets.compare_digest(provided, token):
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                # HTTPS 下必须加 Secure，否则明文端口也能带走这个 Cookie
                secure=_is_https(request),
                samesite="lax",
                max_age=60 * 60 * 24 * 30,
            )
        return response

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "time": time.time()}

    # ---------------- 状态 ----------------

    @app.get("/api/stats", dependencies=[Depends(require_token)])
    async def api_stats() -> dict[str, Any]:
        return control.snapshot()

    @app.get("/api/logs", dependencies=[Depends(require_token)])
    async def api_logs(limit: int = 100) -> dict[str, Any]:
        """优先读日志文件：它比内存缓冲更完整（含进程启动早期的日志）。"""
        try:
            lines = control.logs(limit)
        except Exception:
            lines = []
        if not lines:
            lines = log_buffer.tail(limit)
        return {"lines": lines}

    # ---------------- 源频道 ----------------

    @app.get("/api/sources", dependencies=[Depends(require_token)])
    async def api_sources() -> dict[str, Any]:
        return {"sources": control.sources_info()}

    @app.post("/api/sources", dependencies=[Depends(require_token)])
    async def api_add_source(payload: SourceIn) -> dict[str, Any]:
        """追加一个源（多个源同时监听）。"""
        try:
            return await control.add_source(payload.peer, verify=payload.verify)
        except Exception as exc:
            raise guard(exc) from exc

    @app.post("/api/sources/switch", dependencies=[Depends(require_token)])
    async def api_switch_source(payload: SourceIn) -> dict[str, Any]:
        """换主源。注意：repost 素材在新区道里通常失效，返回里会带提示。"""
        try:
            return await control.switch_source(payload.peer, verify=payload.verify)
        except Exception as exc:
            raise guard(exc) from exc

    @app.delete("/api/sources/{peer}", dependencies=[Depends(require_token)])
    async def api_remove_source(peer: str) -> dict[str, Any]:
        try:
            return await control.remove_source(peer)
        except Exception as exc:
            raise guard(exc) from exc

    # ---------------- 目标群 ----------------

    @app.get("/api/targets", dependencies=[Depends(require_token)])
    async def api_targets() -> dict[str, Any]:
        snapshot = control.snapshot()
        return {"targets": snapshot["targets"], "config": snapshot["config"]["targets"]}

    @app.post("/api/targets", dependencies=[Depends(require_token)])
    async def api_add_target(payload: TargetIn) -> JSONResponse:
        try:
            result = await control.add_target(payload.peer, payload.label, verify=payload.verify)
        except Exception as exc:
            raise guard(exc) from exc
        return JSONResponse({"ok": True, "target": result})

    @app.post("/api/targets/bulk", dependencies=[Depends(require_token)])
    async def api_bulk(payload: BulkIn) -> dict[str, Any]:
        """批量操作（多群管理）。必须定义在 /api/targets/{key} **之前**，否则会被当成 key="bulk"。"""
        try:
            return control.bulk_targets(
                action=payload.action,
                keys=payload.keys,
                daily_limit=payload.daily_limit,
                interval=payload.interval,
            )
        except Exception as exc:
            raise guard(exc) from exc

    @app.patch("/api/targets/{key}", dependencies=[Depends(require_token)])
    async def api_patch_target(key: str, payload: TargetPatch) -> dict[str, Any]:
        try:
            return control.set_target(
                key,
                label=payload.label,
                interval=payload.interval,
                enabled=payload.enabled,
                daily_limit=payload.daily_limit,
            )
        except Exception as exc:
            raise guard(exc) from exc

    @app.delete("/api/targets/{key}", dependencies=[Depends(require_token)])
    async def api_delete_target(key: str) -> dict[str, Any]:
        try:
            return await control.remove_target(key)
        except Exception as exc:
            raise guard(exc) from exc

    @app.post("/api/targets/{key}/pause", dependencies=[Depends(require_token)])
    async def api_pause_target(key: str) -> dict[str, Any]:
        try:
            return control.set_target_paused(key, True)
        except Exception as exc:
            raise guard(exc) from exc

    @app.post("/api/targets/{key}/resume", dependencies=[Depends(require_token)])
    async def api_resume_target(key: str) -> dict[str, Any]:
        try:
            return control.set_target_paused(key, False)
        except Exception as exc:
            raise guard(exc) from exc

    @app.post("/api/targets/{key}/check", dependencies=[Depends(require_token)])
    async def api_check_target(key: str) -> dict[str, Any]:
        try:
            return await control.check_target(key)
        except Exception as exc:
            raise guard(exc) from exc

    @app.post("/api/targets/{key}/probe", dependencies=[Depends(require_token)])
    async def api_probe_target(key: str) -> dict[str, Any]:
        """⚠️ 会真的往群里发一条消息。"""
        try:
            return await control.probe_target(key)
        except Exception as exc:
            raise guard(exc) from exc

    # ---------------- 素材 / 重发 ----------------

    @app.get("/api/materials", dependencies=[Depends(require_token)])
    async def api_materials() -> dict[str, Any]:
        return control.snapshot()["repost"]

    @app.put("/api/materials", dependencies=[Depends(require_token)])
    async def api_set_materials(payload: MaterialsIn) -> dict[str, Any]:
        try:
            return await control.set_materials(payload.ids, apply=payload.apply)
        except Exception as exc:
            raise guard(exc) from exc

    @app.patch("/api/repost", dependencies=[Depends(require_token)])
    async def api_patch_repost(payload: RepostPatch) -> dict[str, Any]:
        try:
            return await control.set_repost_options(**payload.model_dump())
        except Exception as exc:
            raise guard(exc) from exc

    @app.post("/api/repost/run", dependencies=[Depends(require_token)])
    async def api_run_repost() -> dict[str, Any]:
        try:
            return await control.run_repost_once()
        except Exception as exc:
            raise guard(exc) from exc

    @app.post("/api/repost/stop", dependencies=[Depends(require_token)])
    async def api_stop_repost() -> dict[str, Any]:
        try:
            return control.stop_repost()
        except Exception as exc:
            raise guard(exc) from exc

    # ---------------- 限速 ----------------

    @app.patch("/api/rate", dependencies=[Depends(require_token)])
    async def api_patch_rate(payload: RatePatch) -> dict[str, Any]:
        try:
            if payload.global_per_minute is not None:
                if payload.global_per_minute < 1:
                    raise ControlError("global_per_minute 必须大于 0")
                control.sender.pacer.global_per_minute = payload.global_per_minute
                control.sender.pacer._min_spacing = 60.0 / payload.global_per_minute
            if payload.daily_cap is not None:
                if payload.daily_cap < 1:
                    raise ControlError("daily_cap 必须大于 0")
                control.sender.pacer.daily_cap = payload.daily_cap
            control.config_store.set_rate(
                global_per_minute=payload.global_per_minute,
                daily_cap=payload.daily_cap,
            )
            return {
                "global_per_minute": control.sender.pacer.global_per_minute,
                "daily_cap": control.sender.pacer.daily_cap,
            }
        except Exception as exc:
            raise guard(exc) from exc

    # ---------------- 实时推送 ----------------

    @app.websocket("/api/ws")
    async def api_ws(websocket: WebSocket) -> None:
        provided = websocket.query_params.get("token") or websocket.cookies.get(SESSION_COOKIE)
        if not provided or not secrets.compare_digest(provided, token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "snapshot",
                            "data": control.snapshot(),
                            "logs": log_buffer.tail(120),
                        },
                        ensure_ascii=False,
                    )
                )
                await asyncio.sleep(2.0)
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


# --------------------------------------------------------------------------
# 内置单页面板
# --------------------------------------------------------------------------

# 用 raw 字符串：页面里有 JS 正则（如 /[~,\-\s]+/），
# 普通字符串会把 \- 当成非法转义并抛 SyntaxWarning。
_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tg-relay 控制面板</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#262b35;--fg:#e6e8ee;--dim:#8b93a7;
--ok:#3ecf8e;--warn:#e0b341;--bad:#e05c5c;--accent:#5b8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:9}
h1{font-size:16px;margin:0;font-weight:600}
.badge{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
.badge.on{color:var(--ok);border-color:#1d4a35}
.badge.off{color:var(--bad);border-color:#5a2a2a}
main{padding:16px;display:grid;gap:16px;max-width:1200px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:13px;margin:0 0 10px;color:var(--dim);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kv{background:#12151b;border:1px solid var(--line);border-radius:8px;padding:10px}
.kv .k{font-size:11px;color:var(--dim)}
.kv .v{font-size:18px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--dim);font-weight:500;font-size:12px}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
button{background:#222733;color:var(--fg);border:1px solid var(--line);border-radius:6px;
padding:4px 9px;font-size:12px;cursor:pointer}
button:hover{border-color:var(--accent)}
button.danger:hover{border-color:var(--bad);color:var(--bad)}
input,select{background:#12151b;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:13px;width:100%}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row>*{flex:0 0 auto}
.row input.grow{flex:1 1 160px;width:auto}
pre#logs{margin:0;max-height:320px;overflow:auto;background:#0b0d11;border:1px solid var(--line);
border-radius:8px;padding:10px;font-family:ui-monospace,Consolas,monospace;font-size:12px;white-space:pre-wrap}
.empty{color:var(--dim);padding:8px 0}
.dot{display:inline-block;width:8px;height:8px;border-radius:99px;margin-right:6px}
.dot.ok{background:var(--ok)}.dot.paused{background:var(--warn)}.dot.bad{background:var(--bad)}
#toast{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:99}
.toast{background:#1d2230;border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:8px;padding:10px 14px;max-width:420px;font-size:13px}
.toast.err{border-left-color:var(--bad)}
.toast.ok{border-left-color:var(--ok)}
</style>
</head>
<body>
<header>
  <h1>tg-relay 控制面板</h1>
  <span id="mode" class="badge">—</span>
  <span id="conn" class="badge">连接中…</span>
  <span class="badge" id="tokenless"></span>
</header>
<main>
  <section class="card">
    <h2>总览</h2>
    <div class="grid" id="overview"></div>
  </section>

  <section class="card">
    <h2>目标群</h2>
    <table><thead><tr><th>#</th><th>目标</th><th>状态</th><th>发送间隔</th><th>慢速</th><th>今日额度</th><th>积压</th><th>丢弃</th><th style="width:300px">操作</th></tr></thead>
    <tbody id="targets"></tbody></table>
    <div class="row" style="margin-top:12px">
      <input class="grow" id="newPeer" placeholder="@群用户名 或 -1001234567890">
      <input class="grow" id="newLabel" placeholder="备注名（可选）">
      <button onclick="addTarget()">加入目标</button>
    </div>
    <div class="empty" id="targetHint">加目标时会自动探测群慢速并按它的限制对齐发送间隔</div>
  </section>

  <section class="card">
    <h2>批量操作</h2>
    <div class="row">
      <button onclick="bulk('resume')">▶️ 全部恢复</button>
      <button onclick="bulk('pause')">⏸ 全部暂停</button>
    </div>
    <div class="row" style="margin-top:10px">
      <div style="flex:1 1 130px"><div class="empty">每群每日额度（0=用全局）</div>
        <input id="bulkQuota" type="number" min="0" placeholder="如 30"></div>
      <button onclick="bulkLimit()">设为该额度</button>
      <div style="flex:1 1 130px"><div class="empty">总额度平均分</div>
        <input id="bulkShareInput" type="number" min="1" placeholder="如 100"></div>
      <button onclick="bulkShare()">平均分配</button>
    </div>
    <div class="row" style="margin-top:10px">
      <div style="flex:1 1 100px"><div class="empty">间隔下限(秒)</div><input id="bulkLo" type="number" min="1" placeholder="30"></div>
      <div style="flex:1 1 100px"><div class="empty">间隔上限(秒)</div><input id="bulkHi" type="number" min="1" placeholder="35"></div>
      <button onclick="bulkInterval()">统一间隔</button>
    </div>
    <div class="empty" style="margin-top:8px">
      「平均分配」把总额度按群数均分（每群至少 1 条）——多群时避免前面的群吃饱、后面的群饿着
    </div>
  </section>

  <section class="card">
    <h2>定时重发</h2>
    <div class="grid" id="repost"></div>
    <div class="row" style="margin-top:12px">
      <input class="grow" id="matIds" placeholder="素材消息 ID，逗号分隔，如 6,7,8">
      <button onclick="setMaterials()">保存素材</button>
      <button onclick="runRepost()">立刻跑一轮</button>
      <button class="danger" onclick="stopRepost()">停止重发</button>
    </div>
    <table style="margin-top:12px"><thead><tr><th>消息 ID</th><th>内容预览</th><th>已发次数</th></tr></thead>
    <tbody id="materials"></tbody></table>
  </section>

  <section class="card">
    <h2>限速</h2>
    <div class="row">
      <div style="flex:1 1 160px"><div class="empty">全局每分钟上限</div><input id="gpm" type="number" min="1"></div>
      <div style="flex:1 1 160px"><div class="empty">每日转发上限</div><input id="cap" type="number" min="1"></div>
      <button onclick="saveRate()">保存</button>
    </div>
  </section>

  <section class="card">
    <h2>日志</h2>
    <pre id="logs">等待数据…</pre>
  </section>
</main>
<div id="toastBox"></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const H = TOKEN ? {'Authorization':'Bearer '+TOKEN, 'Content-Type':'application/json'} : {'Content-Type':'application/json'};

function toast(msg, kind){
  const box = document.getElementById('toastBox');
  const el = document.createElement('div');
  el.className = 'toast ' + (kind||'');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(()=>el.remove(), 5000);
}
async function api(method, path, body){
  const opt = {method, headers:H};
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch(e){}
  if (!res.ok) throw new Error((data && data.detail) || (method+' '+path+' -> HTTP '+res.status));
  return data;
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
// 数字显示：30.0 -> 30，30.5 -> 30.5（避免表里全是 .0）
function fmtNum(n){ const v = Number(n); return Number.isFinite(v) ? (v % 1 === 0 ? String(v) : v.toFixed(1)) : '?'; }

function render(report){
  const eng = report.engine, snd = report.sender, st = report.store, rp = report.repost;
  document.getElementById('mode').textContent = rp.enabled ? '定时重发' : '实时转发';
  document.getElementById('mode').className = 'badge ' + (rp.enabled ? 'on' : 'off');

  const cards = [
    ['已发送', snd.sent, ''],
    ['失败', snd.failed, snd.failed ? 'bad' : ''],
    ['今日转发', st.sent_today + '/' + snd.daily_cap, ''],
    ['今日重发', st.reposted_today + '/' + (rp.daily_limit||0), ''],
    ['今日实发', st.sent_ok_today, ''],
    ['收到消息', eng.received, ''],
    ['重发轮次', rp.stats ? rp.stats.cycles : 0, ''],
    ['熔断', snd.breaker ? '已触发' : '正常', snd.breaker ? 'bad' : ''],
  ];
  document.getElementById('overview').innerHTML = cards.map(([k,v,cls])=>
    `<div class="kv"><div class="k">${esc(k)}</div><div class="v" style="${cls==='bad'?'color:var(--bad)':''}">${esc(v)}</div></div>`
  ).join('');

  const rpm = report.config.rate;
  if (document.activeElement.id !== 'gpm') document.getElementById('gpm').value = rpm.global_per_minute;
  if (document.activeElement.id !== 'cap') document.getElementById('cap').value = rpm.daily_cap;

  const slow = snd.slow_mode_windows || {};
  const globalInterval = (report.config.rate && report.config.rate.per_target_interval) || null;
  const rows = Object.entries(report.targets || {});
  document.getElementById('targets').innerHTML = rows.length ? rows.map(([name,t],i)=>{
    const state = t.paused ? '<span class="dot paused"></span>暂停' : '<span class="dot ok"></span>正常';
    const secs = slow[name];
    const own = t.daily_limit || 0;
    // 额度：已用/上限，本群单独限额时标出来
    const quota = `${t.quota_used||0}/${t.quota_limit||0}` + (own ? ` <span style="color:var(--accent)">·本群限${own}</span>` : '');
    // 发送间隔：该群自己配了就用它；否则用全局的，加 * 标出来
    let iv;
    if (t.interval && t.interval.length === 2) {
      iv = `${fmtNum(t.interval[0])}~${fmtNum(t.interval[1])}s`;
    } else if (globalInterval) {
      iv = `${fmtNum(globalInterval[0])}~${fmtNum(globalInterval[1])}s<sup>*</sup>`;
    } else {
      iv = '—';
    }
    return `<tr>
      <td>${i+1}</td>
      <td><b>${esc(name)}</b></td>
      <td>${state}</td>
      <td class="mono">${iv}</td>
      <td class="mono">${secs ? Math.round(secs)+'s' : '—'}</td>
      <td class="mono">${quota}</td>
      <td>${t.backlog}</td>
      <td>${t.dropped||0}</td>
      <td>
        <button onclick="setInterval_('${esc(name)}',${JSON.stringify(t.interval || null)})">间隔</button>
        <button onclick="patchTarget('${esc(name)}',{enabled:${t.paused}})">
          ${t.paused?'恢复':'暂停'}</button>
        <button onclick="setQuota('${esc(name)}',${own})">额度</button>
        <button onclick="checkTarget('${esc(name)}')">自检</button>
        <button class="danger" onclick="probeTarget('${esc(name)}')">真发一条</button>
        <button class="danger" onclick="delTarget('${esc(name)}')">移除</button>
      </td></tr>`;
  }).join('') : '<tr><td colspan="9" class="empty">还没有目标群</td></tr>';

  document.getElementById('repost').innerHTML = [
    ['状态', rp.enabled ? '启用' : '关闭'],
    ['间隔', rp.interval ? rp.interval+'s' : '默认'],
    ['日额度', rp.daily_limit],
    ['素材数', (rp.materials||[]).length],
    ['运行中', rp.running ? '是' : '否'],
  ].map(([k,v])=>`<div class="kv"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join('');

  const mats = rp.materials || [];
  document.getElementById('materials').innerHTML = mats.length ? mats.map(m=>
    `<tr><td class="mono">${m.msg_id}</td><td>${esc(m.preview)||'<span class="empty">(非文本)</span>'}</td><td>${m.posted}</td></tr>`
  ).join('') : '<tr><td colspan="3" class="empty">还没有素材</td></tr>';
}

function renderLogs(lines){
  const el = document.getElementById('logs');
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  el.textContent = (lines||[]).join('\\n') || '（暂无日志）';
  if (atBottom) el.scrollTop = el.scrollHeight;
}

let ws = null;
let wsFailures = 0;
let pollTimer = null;

function setConn(text, kind){
  const b = document.getElementById('conn');
  b.textContent = text;
  b.className = 'badge' + (kind ? ' ' + kind : '');
}

function connect(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = proto + '://' + location.host + '/api/ws' + (TOKEN ? '?token='+encodeURIComponent(TOKEN) : '');
  try { ws = new WebSocket(url); }
  catch(e){ scheduleReconnect('无法创建连接'); return; }

  ws.onopen = ()=>{
    wsFailures = 0;
    setConn('已连接', 'on');
    stopPolling();          // 长连接通了就不用再轮询，省请求也省限流额度
  };
  ws.onclose = (ev)=>{
    setConn('断开，重连中', 'off');
    startPolling();         // 长连接断了先用轮询兜底，保证数据仍然在更新
    scheduleReconnect(ev && ev.code ? ('关闭码 ' + ev.code) : '连接关闭');
  };
  ws.onerror = ()=>{ /* onclose 会接着触发，统一在那里处理 */ };
  ws.onmessage = (ev)=>{
    try{
      const msg = JSON.parse(ev.data);
      if (msg.type === 'snapshot'){ render(msg.data); renderLogs(msg.logs); }
    }catch(e){ console.error(e); }
  };
}

// 指数退避：1s, 2s, 4s, 8s, 16s, 30s（封顶）
function scheduleReconnect(reason){
  wsFailures += 1;
  const delay = Math.min(30000, 1000 * Math.pow(2, Math.min(wsFailures - 1, 5)));
  setConn('断开，' + Math.round(delay/1000) + 's 后重连（第 ' + wsFailures + ' 次）', 'off');
  console.warn('WebSocket ' + reason + '，' + delay + 'ms 后重试');
  setTimeout(connect, delay);
}

function startPolling(){
  if (pollTimer) return;
  pollTimer = setInterval(refresh, 10000);
}
function stopPolling(){
  if (pollTimer){ clearInterval(pollTimer); pollTimer = null; }
}

async function refresh(){ try{ render(await api('GET','/api/stats')); }catch(e){ console.warn(e.message); } }
async function addTarget(){
  const peer = document.getElementById('newPeer').value.trim();
  const label = document.getElementById('newLabel').value.trim();
  if (!peer) return toast('请填写目标群', 'err');
  try{
    const r = await api('POST','/api/targets',{peer,label});
    toast(`已加入 ${r.target.label}（慢速 ${r.target.slowmode||'无'}）`, 'ok');
    document.getElementById('newPeer').value=''; document.getElementById('newLabel').value='';
    refresh();
  }catch(e){ toast(e.message,'err'); }
}
async function delTarget(key){
  if (!confirm('移除目标 '+key+'？该群里已发的消息不会删除。')) return;
  try{ await api('DELETE','/api/targets/'+encodeURIComponent(key)); toast('已移除 '+key,'ok'); refresh(); }
  catch(e){ toast(e.message,'err'); }
}
async function patchTarget(key, body){
  try{ await api('PATCH','/api/targets/'+encodeURIComponent(key), body); toast('已更新 '+key,'ok'); refresh(); }
  catch(e){ toast(e.message,'err'); }
}
async function setQuota(key, current){
  const input = prompt('给「'+key+'」设置每日额度（条/天）。0 = 不限，用全局额度。', current || 0);
  if (input === null) return;
  const n = Number(input);
  if (!Number.isFinite(n) || n < 0) return toast('请填 0 或正整数','err');
  await patchTarget(key, {daily_limit: n});
}
async function setInterval_(key, current){
  const shown = current ? `${fmtNum(current[0])}~${fmtNum(current[1])}` : '';
  const input = prompt(
    '给「'+key+'」设置发送间隔（秒），格式：下限~上限，例如 30~35\n' +
    '留空 = 用全局间隔\n' +
    '注意：目标群有慢速模式时，间隔应 >= 慢速值，否则每次都要等慢速窗口',
    shown
  );
  if (input === null) return;
  const text = String(input).trim();
  if (!text) {
    // 清空 = 回落到全局间隔
    try {
      await api('PATCH','/api/targets/'+encodeURIComponent(key), {});
      toast('已清空该群的单独间隔（改用全局值）','ok');
      refresh();
    } catch(e){ toast(e.message,'err'); }
    return;
  }
  const parts = text.split(/[~,\-\s]+/).filter(Boolean).map(Number);
  if (parts.length !== 2 || !parts.every(Number.isFinite)) {
    return toast('格式不对，应该是 30~35 这样','err');
  }
  const lo = Math.min(parts[0], parts[1]);
  const hi = Math.max(parts[0], parts[1]);
  if (lo <= 0) return toast('间隔必须大于 0','err');
  await patchTarget(key, {interval: [lo, hi]});
}
async function bulk(action, extra){
  try{
    const r = await api('POST','/api/targets/bulk', Object.assign({action}, extra||{}));
    toast(`已对 ${r.count} 个目标执行 ${action}`, 'ok');
    refresh();
  }catch(e){ toast(e.message,'err'); }
}
async function bulkLimit(){
  const n = Number(document.getElementById('bulkQuota').value);
  if (!Number.isFinite(n) || n < 0) return toast('请填 0 或正整数','err');
  await bulk('set_limit', {daily_limit: n});
}
async function bulkShare(){
  const n = Number(document.getElementById('bulkShareInput').value);
  if (!Number.isFinite(n) || n < 1) return toast('请填大于 0 的总额度','err');
  try{
    const r = await api('POST','/api/targets/bulk', {action:'share_limit', daily_limit:n});
    const per = r.changed.length ? r.changed[0].daily_limit : 0;
    toast(`总额度 ${n} 已平均分给 ${r.count} 个群，每群 ${per} 条/天`, 'ok');
    refresh();
  }catch(e){ toast(e.message,'err'); }
}
async function bulkInterval(){
  const lo = Number(document.getElementById('bulkLo').value);
  const hi = Number(document.getElementById('bulkHi').value);
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo <= 0 || hi < lo) {
    return toast('间隔不合法：下限需 > 0 且上限 >= 下限','err');
  }
  await bulk('set_interval', {interval: [lo, hi]});
}
async function checkTarget(key){
  try{
    const r = await api('POST','/api/targets/'+encodeURIComponent(key)+'/check');
    toast(`${r.label}：${r.can_post?'可以发送':'不能发送'}${r.slowmode?'（慢速 '+r.slowmode+'s）':''}${r.reason?' — '+r.reason:''}`, r.can_post?'ok':'err');
  }catch(e){ toast(e.message,'err'); }
}
async function probeTarget(key){
  if (!confirm('会真的往 '+key+' 里发一条转发消息，确认？')) return;
  try{
    const r = await api('POST','/api/targets/'+encodeURIComponent(key)+'/probe');
    toast(r.ok ? '可以发送：'+r.note : '不能发送：'+r.note, r.ok?'ok':'err');
  }catch(e){ toast(e.message,'err'); }
}
async function setMaterials(){
  const raw = document.getElementById('matIds').value.trim();
  const ids = raw.split(/[,\\s]+/).filter(Boolean).map(Number).filter(n=>Number.isFinite(n)&&n>0);
  if (!ids.length) return toast('请填写至少一个消息 ID','err');
  try{
    const r = await api('PUT','/api/materials',{ids});
    toast(`素材已更新：${r.ids.join(', ')}（成功加载 ${r.loaded} 条）`,'ok');
    refresh();
  }catch(e){ toast(e.message,'err'); }
}
async function runRepost(){
  try{ const r = await api('POST','/api/repost/run'); toast(`跑完一轮：成功 ${r.sent}，跳过/失败 ${r.skipped_or_failed}`,'ok'); refresh(); }
  catch(e){ toast(e.message,'err'); }
}
async function stopRepost(){
  if (!confirm('停止定时重发循环？（进程继续运行，只是不再重发）')) return;
  try{ await api('POST','/api/repost/stop'); toast('已请求停止重发','ok'); }
  catch(e){ toast(e.message,'err'); }
}
async function saveRate(){
  const gpm = Number(document.getElementById('gpm').value);
  const cap = Number(document.getElementById('cap').value);
  try{ await api('PATCH','/api/rate',{global_per_minute:gpm, daily_cap:cap}); toast('限速已更新','ok'); refresh(); }
  catch(e){ toast(e.message,'err'); }
}

if (!TOKEN) { document.getElementById('tokenless').textContent = 'Cookie 鉴权'; }
connect();
refresh();
// 兜底轮询：WebSocket 一连上就会自动停掉（见 connect/startPolling）
startPolling();
</script>
</body>
</html>
"""


def _default_page(token: str) -> str:
    return _PAGE
