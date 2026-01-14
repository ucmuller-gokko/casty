
import os
import json
import uuid  # ★修正: ここに移動
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, File, UploadFile, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional, Literal
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
import gspread_asyncio
from google.oauth2.service_account import Credentials
import aiohttp
from datetime import datetime
from googleapiclient.discovery import build

# Auth imports
from google_auth_oauthlib.flow import Flow
import google.auth.transport.requests
import google.oauth2.credentials

# --- 設定 ---
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

# --- 環境変数 ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
# ※ OAuth Client Secret が必要です
GOOGLE_OAUTH_CLIENT_SECRET_JSON = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET_JSON") 

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
SHOOTING_CONTACT_SHEET_ID = os.getenv("SHOOTING_CONTACT_SHEET_ID")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL")
SLACK_CHANNEL_TEST = os.getenv("SLACK_CHANNEL_TEST")
SLACK_CHANNEL_TYPE_A = os.getenv("SLACK_CHANNEL_TYPE_A")
SLACK_CHANNEL_TYPE_B = os.getenv("SLACK_CHANNEL_TYPE_B")
SLACK_MENTION_GROUP_ID = os.getenv("SLACK_MENTION_GROUP_ID")
SLACK_CHANNEL_EXTERNAL = os.getenv("SLACK_CHANNEL_EXTERNAL")

CALENDAR_ID_INTERNAL_HOLD = os.getenv("CALENDAR_ID_INTERNAL_HOLD")
GAS_URL_NOTION_SYNC = os.getenv("GAS_URL_NOTION_SYNC")

app = FastAPI()
templates = Jinja2Templates(directory=TEMPLATE_DIR)
slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

# --- Auth Config ---
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/documents',
    'openid'
]

# --- Pydantic Models ---
class OrderItem(BaseModel):
    castingId: str
    roleName: str = ""
    castName: str
    rank: int
    note: str = ""
    projectName: str
    slack_user_id: Optional[str] = None
    conflictInfo: Optional[str] = None
    isInternal: bool = False 

class OrderCreatedPayload(BaseModel):
    accountName: str
    projectName: str
    projectId: str
    dateRanges: List[str]
    orders: List[OrderItem]
    orderType: Literal["pattern_a", "pattern_b", "test"] = "test"
    ccString: Optional[str] = None 
    slackThreadTs: Optional[str] = None
    isAdditionalOrder: bool = False

class StatusUpdatePayload(BaseModel):
    castingId: str
    newStatus: str
    castName: str
    slackThreadTs: Optional[str] = None
    slackPermalink: Optional[str] = None
    extraMessage: Optional[str] = None
    isInternal: Optional[bool] = False
    projectId: Optional[str] = None
    mainSub: Optional[str] = "その他"
    orderDetails: Optional[list] = None
    
    class Config:
        extra = "ignore"

class ShootingContactUpdateItem(BaseModel):
    castingId: str
    status: Optional[str] = None
    inTime: Optional[str] = None
    outTime: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    cost: Optional[str] = None
    makingUrl: Optional[str] = None
    postDate: Optional[str] = None
    mainSub: Optional[str] = None
    poUuid: Optional[str] = None

class SpecialOrderPayload(BaseModel):
    orderType: Literal["external", "internal"]
    title: str
    dates: List[str]
    startTime: str
    endTime: str
    castIds: List[str]
    ordererEmail: str

class SpecialOrderUpdatePayload(BaseModel):
    slackThreadTs: str
    title: Optional[str] = None
    dates: Optional[List[str]] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    castIds: Optional[List[str]] = None

class SpecialOrderDeletePayload(BaseModel):
    slackThreadTs: str

class OrderDeletePayload(BaseModel):
    castingId: str
    slackThreadTs: str = ""

# --- Helpers ---
def get_creds():
    creds_json_str = os.getenv("GOOGLE_SHEETS_CREDS_JSON")
    if not creds_json_str:
        json_path = os.path.join(BASE_DIR, 'service_account.json')
        if os.path.exists(json_path):
             scopes = [
                 "https://www.googleapis.com/auth/spreadsheets",
                 "https://www.googleapis.com/auth/drive",
                 "https://www.googleapis.com/auth/calendar"  # カレンダー権限追加
             ]
             return Credentials.from_service_account_file(json_path, scopes=scopes)
        raise ValueError("環境変数 'GOOGLE_SHEETS_CREDS_JSON' が設定されていません。")
    
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/calendar"  # カレンダー権限追加
    ]
    creds_dict = json.loads(creds_json_str)
    return Credentials.from_service_account_info(creds_dict, scopes=scopes)

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

def get_client_config():
    env_json = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET_JSON")
    if env_json:
        try:
            return json.loads(env_json)
        except json.JSONDecodeError:
            print("Error: GOOGLE_OAUTH_CLIENT_SECRET_JSON is invalid JSON")
    
    json_path = os.path.join(BASE_DIR, 'client_secret.json')
    if os.path.exists(json_path):
        with open(json_path, 'r') as f:
            return json.load(f)
            
    raise ValueError("Client Secretが見つかりません。")

def pick_channel(order_type: str) -> str:
    if order_type == "pattern_a": return SLACK_CHANNEL_TYPE_A or SLACK_DEFAULT_CHANNEL
    if order_type == "pattern_b": return SLACK_CHANNEL_TYPE_B or SLACK_DEFAULT_CHANNEL
    return SLACK_CHANNEL_TEST or SLACK_DEFAULT_CHANNEL or ""

def build_order_text(payload: OrderCreatedPayload, upload_error: str = None) -> str:
    lines = []
    if SLACK_MENTION_GROUP_ID:
        lines.append(f"<!subteam^{SLACK_MENTION_GROUP_ID}>")
    
    if payload.ccString:
        lines.append(f"cc: {payload.ccString}")

    lines.append("キャスティングオーダーがありました。")
    
    # Check if any order is internal
    has_internal = any(o.isInternal for o in payload.orders)

    if not payload.isAdditionalOrder and has_internal:
        lines.append("*内部キャストはスタンプで反応ください*")

    if payload.isAdditionalOrder:
        lines = []
        if SLACK_MENTION_GROUP_ID:
            lines.append(f"<!subteam^{SLACK_MENTION_GROUP_ID}>")
        
        lines.append("追加オーダーのお知らせ")
        if has_internal:
            lines.append("*内部キャストはスタンプで反応ください*")
        lines.append("")

        projects = {}
        project_ordered = []
        for order in payload.orders:
            if order.projectName not in projects:
                projects[order.projectName] = {}
                project_ordered.append(order.projectName)
            if order.roleName not in projects[order.projectName]:
                projects[order.projectName][order.roleName] = []
            projects[order.projectName][order.roleName].append(order)
        
        for p_name in project_ordered:
            lines.append(f"【{p_name}】")
            for r_name, cands in projects[p_name].items():
                # Fix: Use mentions if available
                cast_disp_list = []
                for c in cands:
                    if c.slack_user_id:
                        cast_disp_list.append(f"<@{c.slack_user_id}>")
                    else:
                        cast_disp_list.append(c.castName)
                
                cast_names = " / ".join(cast_disp_list)
                lines.append(f"{r_name}：{cast_names}")
                
                for c in cands:
                    if c.conflictInfo:
                        lines.append(f"  🚨 {c.conflictInfo}")
            lines.append("")

        if upload_error:
            lines.append(f"\n⚠️ PDF送信エラー: {upload_error}")

        return "\n".join(lines).rstrip()
    
    if upload_error:
        lines.append("")
        lines.append("⚠️ **PDF送信に失敗したので、Slackにて手動での添付をお願いします**")
        lines.append(f"Reason: {upload_error}")
    
    lines.append("")
    lines.append("`撮影日`")
    for d in payload.dateRanges:
        lines.append(f"・{d}")
    lines.append("")

    lines.append("`アカウント`")
    lines.append(payload.accountName or "未入力")
    lines.append("")

    projects = {}
    project_ordered = []
    for order in payload.orders:
        if order.projectName not in projects:
            projects[order.projectName] = {}
            project_ordered.append(order.projectName)
        if order.roleName not in projects[order.projectName]:
            projects[order.projectName][order.roleName] = []
        projects[order.projectName][order.roleName].append(order)

    lines.append("`作品名`")
    lines.append("/".join(project_ordered) if project_ordered else "未定")
    lines.append("")

    lines.append("`役名`")
    for p_name in project_ordered:
        lines.append(f"【{p_name}】") 
        for r_name, cands in projects[p_name].items():
            lines.append(f"  {r_name}")
            cands.sort(key=lambda x: x.rank)
            for cand in cands:
                cast_disp = f"<@{cand.slack_user_id}>" if cand.slack_user_id else cand.castName
                line = f"    第{cand.rank}候補：{cast_disp}"
                lines.append(line)
                if cand.conflictInfo:
                    lines.append(f"    🚨 {cand.conflictInfo}")

    lines.append("")
    lines.append("`Notionリンク`")
    if payload.projectId:
        lines.append(f"https://www.notion.so/{payload.projectId.replace('-', '')}")
    else:
        lines.append("未設定")

    lines.append("\n--------------------------------------------------")
    return "\n".join(lines).rstrip()

def build_status_update_text(payload: StatusUpdatePayload) -> str:
    status = payload.newStatus
    cast_name = payload.castName
    extra_message = payload.extraMessage

    if status == "追加オーダー":
        return f"追加オーダーが登録されました。\n{extra_message or ''}".rstrip()

    base = f"{cast_name}さん、出演{status}でした。"
    if extra_message:
        return base + "\n" + extra_message
    return base

async def sync_to_notion_via_gas(payload: StatusUpdatePayload):
    if not GAS_URL_NOTION_SYNC:
        print("GAS_URL_NOTION_SYNC is not set.")
        return

    gas_payload = {
        "pageId": payload.projectId,
        "castName": payload.castName,
        "isInternal": payload.isInternal,
        "orderDetails": payload.orderDetails 
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GAS_URL_NOTION_SYNC, json=gas_payload) as resp:
                if resp.status == 200:
                    print(f"Notion sync success: {payload.castName}")
                else:
                    text = await resp.text()
                    print(f"Notion sync failed: {text}")
    except Exception as e:
        print(f"Notion sync exception: {e}")

# --- Auth Endpoints ---

@app.post("/api/auth/login")
async def auth_login(request: Request):
    try:
        data = await request.json()
        auth_code = data.get("code")
        if not auth_code:
            raise HTTPException(status_code=400, detail="No code provided")

        client_config = get_client_config()

        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri="postmessage"
        )
        flow.fetch_token(code=auth_code)
        creds = flow.credentials

        response = JSONResponse({"ok": True, "access_token": creds.token})
        
        if creds.refresh_token:
            response.set_cookie(
                key="refresh_token",
                value=creds.refresh_token,
                httponly=True,
                secure=True,
                samesite="lax",
                max_age=86400 # 1 day
            )
        return response
    except Exception as e:
        print(f"Login error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/auth/refresh")
async def auth_refresh(request: Request):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No session")

    try:
        client_config = get_client_config()
        
        creds = google.oauth2.credentials.Credentials(
            None,
            refresh_token=refresh_token,
            token_uri=client_config["web"]["token_uri"],
            client_id=client_config["web"]["client_id"],
            client_secret=client_config["web"]["client_secret"],
            scopes=SCOPES
        )
        
        req = google.auth.transport.requests.Request()
        creds.refresh(req)
        
        return {"ok": True, "access_token": creds.token}
    except Exception as e:
        print(f"Refresh failed: {e}")
        res = JSONResponse({"ok": False}, status_code=401)
        res.delete_cookie("refresh_token")
        return res

@app.post("/api/auth/logout")
async def auth_logout():
    res = JSONResponse({"ok": True})
    res.delete_cookie("refresh_token")
    return res

# --- API Endpoints ---
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

@app.get("/config")
async def get_config():
    return {
        "calendar_id_internal_hold": CALENDAR_ID_INTERNAL_HOLD,
        "slack_default_channel": SLACK_DEFAULT_CHANNEL,
    }

@app.post("/api/notify/order_created")
async def notify_order_created(
    files: List[UploadFile] = File(None), 
    payload_str: str = Form(...)
):
    try:
        data = json.loads(payload_str)
        payload = OrderCreatedPayload(**data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payload error: {e}")

    if not SLACK_BOT_TOKEN or not slack_client:
        raise HTTPException(status_code=500, detail="Slack Config Error")

    channel = pick_channel(payload.orderType)
    
    ts = None
    permalink = ""
    upload_error = None
    sent_via_upload = False

    if files and len(files) > 0:
        upload_list = []
        for file in files:
            await file.seek(0)
            content = await file.read()
            upload_list.append({
                "file": content,
                "filename": file.filename,
                "title": file.filename
            })
        
        try:
            initial_text = build_order_text(payload)
            response = await slack_client.files_upload_v2(
                channel=channel,
                initial_comment=initial_text,
                file_uploads=upload_list,
                thread_ts=payload.slackThreadTs
            )
            sent_via_upload = True
            
            # レスポンスからtsとpermalinkを取得（複数のフォールバック）
            if hasattr(response, 'data') and isinstance(response.data, dict):
                files_resp = response.data.get("files", [])
                print(f"📁 files_upload_v2 returned {len(files_resp)} file(s)")
                
                if files_resp:
                    file_info = files_resp[0]
                    shares = file_info.get("shares", {})
                    
                    # 方法1: shares.public または shares.private からts取得
                    for share_type in ["public", "private"]:
                        if share_type in shares:
                            for ch_id, share_list in shares[share_type].items():
                                if share_list and "ts" in share_list[0]:
                                    ts = share_list[0]["ts"]
                                    print(f"✅ Got ts from shares.{share_type}: {ts}")
                                    break
                        if ts:
                            break
                    
                    # 方法2: tsが取れなかった場合、ファイルのpermalinkからts抽出
                    if not ts and "permalink" in file_info:
                        file_permalink = file_info["permalink"]
                        # ファイルpermalinkからメッセージpermalinkへの変換は難しいが、
                        # ファイルが共有されたメッセージのtsはshares内にあるはず
                        print(f"⚠️ ts not found in shares, file permalink: {file_permalink}")
                    
                    # 方法3: shares内のどこかにtsがあれば取得
                    if not ts:
                        for f in files_resp:
                            if "shares" in f:
                                for share_type in ["public", "private"]:
                                    type_shares = f["shares"].get(share_type, {})
                                    for ch_id, share_list in type_shares.items():
                                        if share_list and "ts" in share_list[0]:
                                            ts = share_list[0]["ts"]
                                            print(f"✅ Got ts from nested shares: {ts}")
                                            break
                                    if ts:
                                        break
                            if ts:
                                break
                    
                    # 方法4: sharesが空の場合、files.infoで再取得
                    if not ts and files_resp:
                        file_id = files_resp[0].get("id")
                        if file_id:
                            print(f"🔍 Fetching file info for: {file_id}")
                            try:
                                import asyncio
                                # 少し待ってからfiles.infoを取得（ファイル共有の処理待ち）
                                await asyncio.sleep(2.0)  # 2秒待機
                                file_info_resp = await slack_client.files_info(file=file_id)
                                print(f"📄 files.info response received")
                                if file_info_resp and "file" in file_info_resp:
                                    file_data = file_info_resp["file"]
                                    shares = file_data.get("shares", {})
                                    print(f"   Shares keys: {list(shares.keys())}")
                                    for share_type in ["public", "private"]:
                                        if share_type in shares:
                                            for ch_id, share_list in shares[share_type].items():
                                                print(f"   {share_type}[{ch_id}]: {share_list}")
                                                if share_list and "ts" in share_list[0]:
                                                    ts = share_list[0]["ts"]
                                                    print(f"✅ Got ts from files.info: {ts}")
                                                    break
                                        if ts:
                                            break
                            except Exception as file_info_err:
                                print(f"⚠️ files.info failed: {file_info_err}")
                    
                    if not ts:
                        print(f"⚠️ Could not extract ts. Response keys: {list(response.data.keys())}")
                        print(f"   File shares: {shares}")

        except Exception as e:
            print(f"❌ PDF Upload Failed: {e}")
            upload_error = str(e)

    if not sent_via_upload:
        fallback_text = build_order_text(payload, upload_error)
        try:
            res = await slack_client.chat_postMessage(
                channel=channel,
                text=fallback_text,
                thread_ts=payload.slackThreadTs
            )
            ts = res.get("ts")
            print(f"✅ Fallback message sent, ts: {ts}")
        except Exception as e:
            print(f"❌ Text Message Failed: {e}")
            raise HTTPException(status_code=500, detail="Slack送信失敗")

    if ts:
        try:
            perm = await slack_client.chat_getPermalink(channel=channel, message_ts=ts)
            permalink = perm.get("permalink", "")
            print(f"✅ Got permalink: {permalink}")
        except Exception as e:
            print(f"⚠️ getPermalink failed: {e}")

    return {"ok": True, "ts": ts, "permalink": permalink, "upload_error": upload_error}


@app.post("/api/notify/special_order")
async def notify_special_order(payload: SpecialOrderPayload):
    if not SLACK_BOT_TOKEN or not slack_client:
        raise HTTPException(status_code=500, detail="Slack BOT TOKEN未設定")

    try:
        creds = get_creds()
        agcm_local = gspread_asyncio.AsyncioGspreadClientManager(lambda: creds)
        gc = await agcm_local.authorize()
        sh = await gc.open_by_key(SPREADSHEET_ID)
        ws = await sh.worksheet("キャスティングリスト")

        cast_map = {}
        email_to_slack_map = {}

        try:
            internal_ws = await sh.worksheet("内部キャストDB")
            internal_rows = await internal_ws.get_all_values()
            for row in internal_rows[1:]:
                if len(row) < 5: continue
                email = str(row[3]).strip()
                slack_id = str(row[4]).strip()
                if email:
                    email_to_slack_map[email.lower()] = slack_id
        except Exception as e:
            print(f"Warning: Failed to load Internal Cast DB: {e}")

        try:
            try:
                external_ws = await sh.worksheet("キャストリスト")
            except:
                external_ws = await sh.worksheet("CastDB")
            
            external_rows = await external_ws.get_all_values()
            for row in external_rows[1:]:
                if len(row) < 2: continue
                cid = str(row[0]).strip()
                name = str(row[1]).strip()
                email = str(row[7]).strip() if len(row) > 7 else ""
                slack_id = str(row[10]).strip() if len(row) > 10 else ""
                type_val = str(row[9]).strip() if len(row) > 9 else "外部"

                if cid:
                    cast_map[cid] = {
                        "name": name,
                        "email": email,
                        "slack_id": slack_id,
                        "type": type_val
                    }
                    if email:
                         email_to_slack_map[email.lower()] = slack_id
        except Exception as e:
            print(f"Warning: Failed to load Cast List: {e}")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_rows = []
        internal_events = []
        
        account_name = "外部案件" if payload.orderType == "external" else "社内イベント"

        # 統合Slackメッセージを作成（全キャストをまとめて）
        orderer_email_key = payload.ordererEmail.strip().lower()
        cc_slack_id = email_to_slack_map.get(orderer_email_key)
        cc_mention = f"<@{cc_slack_id}>" if cc_slack_id else payload.ordererEmail

        dates_str = ", ".join(payload.dates).replace("-", "/")
        time_range = f"{payload.startTime} ~ {payload.endTime}"
        
        # メッセージ構築
        msg_lines = []
        msg_lines.append(f"【{account_name}】")
        msg_lines.append(f"`タイトル`\n{payload.title}")
        msg_lines.append(f"`日時`\n{dates_str}")
        msg_lines.append(f"`時間`\n{time_range}")
        msg_lines.append("")
        msg_lines.append("`キャスト`")
        
        # 全キャストをリスト表示
        for cid in payload.castIds:
            cid_str = str(cid).strip()
            cast = cast_map.get(cid_str, {})
            cast_name = cast.get("name") or "不明"
            slack_id = cast.get("slack_id") or ""
            mention = f"<@{slack_id}>" if slack_id else cast_name
            cast_type = cast.get("type") or "外部"
            type_label = "（内部）" if cast_type == "内部" else ""
            msg_lines.append(f"・{mention} {type_label}")
        
        msg_lines.append("")
        msg_lines.append(f"CC: {cc_mention}")
        
        msg = "\n".join(msg_lines)

        # 1つのSlack投稿を送信
        ts = None
        permalink = ""
        target_channel = SLACK_DEFAULT_CHANNEL

        if payload.orderType == "external":
            if SLACK_CHANNEL_EXTERNAL:
                target_channel = SLACK_CHANNEL_EXTERNAL

        try:
            resp = await slack_client.chat_postMessage(
                channel=target_channel,
                text=msg
            )
            ts = resp.get("ts")
            if ts:
                perm = await slack_client.chat_getPermalink(channel=target_channel, message_ts=ts)
                permalink = perm.get("permalink", "")
        except Exception as e:
            print(f"Slack error: {e}")

        # 全キャスト×全日付分のレコードを作成（同じts、permalinkを使用）
        for cid in payload.castIds:
            cid_str = str(cid).strip()
            cast = cast_map.get(cid_str, {})
            cast_name = cast.get("name") or "不明"
            cast_email = cast.get("email") or ""
            cast_type = cast.get("type") or "外部"
            is_internal_cast = (cast_type == "内部")
            status = "仮キャスティング" if is_internal_cast else "決定"

            for date in payload.dates:
                casting_id = f"sp_{uuid.uuid4()}"
                
                row = [
                    casting_id,             # A
                    account_name,           # B
                    payload.title,          # C
                    "出演",                 # D
                    cid_str,                # E
                    cast_name,              # F
                    date,                   # G
                    date,                   # H
                    1,                      # I
                    status,                 # J
                    f"{time_range}",        # K: 時間範囲（time_rangeフィールド）
                    f"'{ts}",                # L: SlackスレッドID（シングルクォート付きで小数点保持）
                    permalink,              # M: 全レコードで同じpermalink
                    "その他",               # N
                    "",                     # O
                    "",                     # P
                    timestamp,              # Q
                    payload.ordererEmail,   # R
                    "",                     # S
                    cast_type,              # T
                    cast_email,             # U
                    "",                     # V
                    "[]"                    # W
                ]
                new_rows.append(row)

                if is_internal_cast:
                    internal_events.append({
                        "castingId": casting_id,
                        "accountName": account_name,
                        "projectName": payload.title,
                        "roleName": "出演",
                        "mainSub": "その他",
                        "start": date,
                        "end": date,
                        "email": cast_email,
                        "status": status,
                        "time_range": time_range,  # 時間情報を渡す
                        "rowNumber": None
                    })

        response_data = {"ok": True, "calendar_events": []}

        if new_rows:
            append_res = await ws.append_rows(new_rows, value_input_option="USER_ENTERED")
            
            if internal_events:
                updated_range = append_res.get('updates', {}).get('updatedRange', '')
                import re
                match = re.search(r'!A(\d+):', updated_range)
                start_row = int(match.group(1)) if match else 0
                
                if start_row > 0:
                    id_to_row = {}
                    for i, r in enumerate(new_rows):
                        id_to_row[r[0]] = start_row + i
                    for ev in internal_events:
                        if ev["castingId"] in id_to_row:
                            ev["rowNumber"] = id_to_row[ev["castingId"]]
                    response_data["calendar_events"] = internal_events

    except Exception as e:
        print(f"Error in notify_special_order: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return response_data


@app.post("/api/notify/status_update")
async def notify_status_update(
    payload: StatusUpdatePayload,
    background_tasks: BackgroundTasks
):
    if not SLACK_BOT_TOKEN or not slack_client:
        print("Slack token not set, skipping notification.")
    
    if payload.newStatus in ["OK", "決定"]:
        if payload.projectId and payload.castName:
             background_tasks.add_task(sync_to_notion_via_gas, payload)

    if not payload.slackThreadTs:
        return JSONResponse(content={"ok": True, "message": "DB append only"})

    channel = SLACK_CHANNEL_TEST or SLACK_DEFAULT_CHANNEL
    if not channel:
        raise HTTPException(status_code=500, detail="Slack通知先チャンネルが未設定です。")

    if payload.newStatus == "追加オーダー":
        text = f"追加オーダーが登録されました。\n{payload.extraMessage or ''}"
    else:
        text = build_status_update_text(payload)

    try:
        res = await slack_client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=payload.slackThreadTs,
            unfurl_links=False,
            unfurl_media=False,
        )
        return JSONResponse(content={"ok": True})
    except SlackApiError as e:
        print(f"Slack API Error (status_update): {e.response['error']}")
        raise HTTPException(status_code=500, detail=f"Slack通知の送信に失敗しました: {e.response['error']}")
    except Exception as e:
        print(f"Unexpected error on status_update: {e}")
        raise HTTPException(status_code=500, detail="ステータス更新Slack通知で予期せぬエラーが発生しました。")

@app.get("/api/shooting_contact/list")
# main.py の既存の shooting_contact_list をこれに置き換えてください

@app.get("/api/shooting_contact/list")
async def shooting_contact_list():
    # ID設定チェック
    if not SHOOTING_CONTACT_SHEET_ID:
        return JSONResponse(status_code=500, content={"detail": "SHOOTING_CONTACT_SHEET_ID が .env に設定されていません"})
    
    try:
        # 1. 認証チェック
        client = await agcm.authorize()
        
        # 2. スプレッドシートを開く
        try:
            ss = await client.open_by_key(SHOOTING_CONTACT_SHEET_ID)
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"スプレッドシートが開けません (ID間違い or 権限不足): {str(e)}"})

        # 3. ワークシートを開く
        try:
            sheet = await ss.worksheet("撮影連絡DB")
        except Exception as e:
            # シート一覧を取得してみる（デバッグ用）
            all_sheets = [ws.title for ws in await ss.worksheets()]
            return JSONResponse(status_code=500, content={
                "detail": f"「撮影連絡DB」シートが見つかりません: {str(e)}",
                "existing_sheets": all_sheets
            })

        # 4. データ取得
        values = await sheet.get_all_values()

        if not values or len(values) < 2:
            return []

        rows = values[1:]
        result = []
        for r in rows:
            result.append({
                "castingId": r[0] if len(r) > 0 else "",
                "accountName": r[1] if len(r) > 1 else "",
                "projectName": r[2] if len(r) > 2 else "",
                "notionId": r[3] if len(r) > 3 else "",
                "roleName": r[4] if len(r) > 4 else "",
                "castName": r[5] if len(r) > 5 else "",
                "castType": r[6] if len(r) > 6 else "",
                "shootDate": r[7] if len(r) > 7 else "",
                "note": r[8] if len(r) > 8 else "",
                "status": r[9] if len(r) > 9 else "",
                "inTime": r[10] if len(r) > 10 else "",
                "outTime": r[11] if len(r) > 11 else "",
                "location": r[12] if len(r) > 12 else "",
                "address": r[13] if len(r) > 13 else "",
                "makingUrl": r[14] if len(r) > 14 else "",
                "cost": r[15] if len(r) > 15 else "",
                "postDate": r[16] if len(r) > 16 else "",
                "updatedBy": r[17] if len(r) > 17 else "",
                "updatedAt": r[18] if len(r) > 18 else "",
                "mainSub": r[19] if len(r) > 19 else "その他",
                "poUuid": r[20] if len(r) > 20 else "",
            })
        return result

    except Exception as e:
        import traceback
        print("Detailed Error:", traceback.format_exc())
        return JSONResponse(status_code=500, content={"detail": f"予期せぬエラー: {str(e)}", "trace": traceback.format_exc()})
@app.post("/api/shooting_contact/add")
async def add_shooting_contact(payload: dict):
    if not SHOOTING_CONTACT_SHEET_ID:
        raise HTTPException(status_code=500, detail="SHOOTING_CONTACT_SHEET_ID missing")

    required = [
        "castingId", "account", "projectName", "notionId",
        "roleName", "castName", "castType", "shootDate"
    ]
    for r in required:
        if r not in payload:
            raise HTTPException(status_code=400, detail=f"Missing field: {r}")

    row = [
        payload["castingId"],        # A
        payload["account"],          # B
        payload["projectName"],      # C
        payload["notionId"],         # D
        payload["roleName"],         # E
        payload["castName"],         # F
        payload["castType"],         # G
        payload["shootDate"],        # H
        payload.get("note", ""),     # I
        "香盤連絡待ち",               # J
        payload.get("inTime", ""),   # K
        payload.get("outTime", ""),  # L
        payload.get("location", ""), # M
        payload.get("address", ""),  # N
        payload.get("makingUrl", ""),# O
        payload.get("cost", ""),     # P (15) ★追加（金額）
        payload.get("postDate", ""), # Q (16) 旧P
        payload.get("updatedBy", ""),# R (17) 旧Q
        payload.get("updatedAt", ""),# S (18) 旧R
        payload.get("mainSub", "その他"), # T (19) 旧S
    ]

    try:
        client = await agcm.authorize()
        ss = await client.open_by_key(SHOOTING_CONTACT_SHEET_ID)
        sheet = await ss.worksheet("撮影連絡DB")

        await sheet.append_row(row, value_input_option="USER_ENTERED")
        return {"ok": True}

    except Exception as e:
        print(f"Error in add_shooting_contact: {e}")
        raise HTTPException(status_code=500, detail=f"append failed: {e}")

@app.post("/api/shooting_contact/update")
async def update_shooting_contact_status(payload: ShootingContactUpdateItem):
    if not SHOOTING_CONTACT_SHEET_ID:
        raise HTTPException(status_code=500, detail="SHOOTING_CONTACT_SHEET_ID missing")

    try:
        client = await agcm.authorize()
        ss = await client.open_by_key(SHOOTING_CONTACT_SHEET_ID)
        sheet = await ss.worksheet("撮影連絡DB")
        
        col_a = await sheet.col_values(1)
        
        try:
            row_idx = col_a.index(payload.castingId) + 1
        except ValueError:
            raise HTTPException(status_code=404, detail="Casting ID not found in DB")
        
        # 前のステータスを取得（マスターデータ更新判定用）
        prev_status = None
        if payload.status is not None:
            current_row = await sheet.row_values(row_idx)
            prev_status = current_row[9] if len(current_row) > 9 else None  # J列
        
        updates = []
        if payload.status is not None:
            updates.append({"range": f"J{row_idx}", "values": [[payload.status]]})
        if payload.inTime is not None:
            updates.append({"range": f"K{row_idx}", "values": [[payload.inTime]]})
        if payload.outTime is not None:
            updates.append({"range": f"L{row_idx}", "values": [[payload.outTime]]})
        if payload.location is not None:
            updates.append({"range": f"M{row_idx}", "values": [[payload.location]]})
        if payload.address is not None:
            updates.append({"range": f"N{row_idx}", "values": [[payload.address]]})
        if payload.makingUrl is not None:
            updates.append({"range": f"O{row_idx}", "values": [[payload.makingUrl]]})
        if payload.cost is not None:
            updates.append({"range": f"P{row_idx}", "values": [[payload.cost]]})
        if payload.postDate is not None:
            updates.append({"range": f"Q{row_idx}", "values": [[payload.postDate]]})
        if payload.mainSub is not None:
            updates.append({"range": f"T{row_idx}", "values": [[payload.mainSub]]})
        if payload.poUuid is not None:
            updates.append({"range": f"U{row_idx}", "values": [[payload.poUuid]]})

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates.append({"range": f"S{row_idx}", "values": [[now_str]]})
        
        if updates:
            await sheet.batch_update(updates)
        
        # マスターデータ更新（メイキング共有待ち→投稿日連絡待ち）
        if prev_status == "メイキング共有待ち" and payload.status == "投稿日連絡待ち":
            print(f"🔄 Status changed to 投稿日連絡待ち, syncing master data for {payload.castingId}")
            try:
                master_ss = await client.open_by_key(SPREADSHEET_ID)
                master_ws = await master_ss.worksheet("マスターデータ")
                master_col_a = await master_ws.col_values(1)
                
                try:
                    master_row_idx = master_col_a.index(payload.castingId) + 1
                    master_updates = []
                    if payload.poUuid is not None:
                        master_updates.append({"range": f"P{master_row_idx}", "values": [[payload.poUuid]]})
                    if payload.cost is not None:
                        master_updates.append({"range": f"O{master_row_idx}", "values": [[payload.cost]]})
                    if master_updates:
                        await master_ws.batch_update(master_updates)
                        print(f"✅ Updated master data: poId={payload.poUuid}, amount={payload.cost}")
                except ValueError:
                    print(f"⚠️ CastingID {payload.castingId} not found in master data")
            except Exception as e:
                print(f"Error updating master data: {e}")
        
        return {"ok": True}

    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error in update_shooting_contact_status: {e}")
        raise HTTPException(status_code=500, detail=f"Update failed: {e}")

@app.post("/api/sync/gas")
async def sync_gas_trigger(type: str = "schedule"):
    if type == "schedule":
        gas_url = "https://script.google.com/macros/s/AKfycbxN-wyoTYcLIAIVzp3gOwNIFUK02a1iGeV_-VPJlXKXx8bimlMe3oTDutljnGc8Xrkn/exec"
    elif type == "making":
        gas_url = "https://script.google.com/macros/s/AKfycbxi2abt-T0FnzW2n5OvcwKNImlLLD0qqB5rZARO1kc9EuXXz342ee_11Ypnr56N3ap6/exec"
    elif type == "post_date":
        gas_url = os.getenv("GAS_URL_POST_DATE", "")
    else:
        raise HTTPException(status_code=400, detail=f"Invalid sync type: {type}")

    if not gas_url:
        raise HTTPException(status_code=501, detail=f"GAS URL for '{type}' is not configured yet.")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(gas_url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print(f"GAS Error: {resp.status} - {text}")
                    raise HTTPException(status_code=500, detail=f"GAS execution failed: {resp.status}")
                
                data = await resp.json()
                return {"ok": True, "gas_response": data}
    except Exception as e:
        print(f"Sync GAS Error: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
        "SPREADSHEET_ID": SPREADSHEET_ID,
        "GOOGLE_API_KEY": GOOGLE_API_KEY,
    })

# 特別オーダー編集・削除API
# main.py の update_special_order 関数全体をこれに置き換えてください

# main.py の update_special_order 関数をこれに置き換えてください

@app.post("/api/special_order/update")
async def update_special_order(payload: SpecialOrderUpdatePayload):
    """
    特別オーダーの更新
    詳細なデバッグログを出力し、検索プロセスを可視化します
    """
    # [Log 1] リクエスト到達確認
    print("\n" + "="*50)
    print("🚀 API Call: /api/special_order/update")
    print(f"📥 Received Payload TS: '{payload.slackThreadTs}' ({type(payload.slackThreadTs)})")
    print(f"📥 Received Title: '{payload.title}'")
    print("="*50)

    if not payload.slackThreadTs:
        print("❌ Error: slackThreadTs is missing/empty")
        raise HTTPException(status_code=400, detail="slackThreadTs required")
    
    try:
        print("🔹 Connecting to Google Sheets...")
        creds = get_creds()
        agcm_local = gspread_asyncio.AsyncioGspreadClientManager(lambda: creds)
        gc = await agcm_local.authorize()
        sh = await gc.open_by_key(SPREADSHEET_ID)
        ws = await sh.worksheet("キャスティングリスト")
        print("✅ Sheet connected.")
        
        # 全データ取得
        print("🔹 Fetching all values from sheet...")
        all_rows = await ws.get_all_values()
        total_rows = len(all_rows)
        print(f"✅ Fetched {total_rows} rows.")
        
        target_rows = []       # 更新対象の行番号とデータ
        calendar_event_ids = [] # 更新対象のカレンダーイベントID
        
        # 検索対象のID (文字列化 & 空白削除)
        search_ts_str = str(payload.slackThreadTs).strip()

        print(f"🔹 Starting Search Loop. Target TS: [{search_ts_str}]")

        # --- 比較用ヘルパー関数 ---
        def is_match(sheet_val, search_val_str):
            """
            SlackThreadTsを四捨五入した値で比較
            例: 1767953327.818619 → 1767953328
            """
            try:
                # 両方を文字列化
                sheet_str = str(sheet_val).strip()
                search_str = str(search_val_str).strip()
                
                # シングルクォート除去
                if sheet_str.startswith("'"):
                    sheet_str = sheet_str[1:]
                
                # 完全一致で比較
                if sheet_str == search_str:
                    print(f"   ✅ MATCH: Sheet[{sheet_str}] == Target[{search_str}]")
                    return True
                
                return False
            except Exception as e:
                print(f"   ⚠️ Comparison error: {e}")
                return False
        # ------------------------

        # 検索ループ
        for i, row in enumerate(all_rows[1:], start=2):
            # L列(インデックス11) が ThreadTs
            if len(row) > 11:
                current_ts = row[11]
                
                # デバッグ: 最初の数行だけL列の中身を表示してみる
                if i < 7: 
                    print(f"   Row {i} L-col: '{current_ts}'")

                if is_match(current_ts, search_ts_str):
                    print(f"✅ FOUND at Row {i}!")
                    target_rows.append((i, row))
                    # O列(インデックス14) が CalendarEventId
                    if len(row) > 14 and row[14]:
                        calendar_event_ids.append(row[14])
        
        print(f"🔹 Search finished. Found {len(target_rows)} matching rows.")

        if not target_rows:
            print("❌ Order not found. Dumping L-col samples for debugging:")
            # 見つからなかった場合、L列の値をいくつかダンプ
            for k, r in enumerate(all_rows[1:10], start=2):
                 val = r[11] if len(r) > 11 else "(Empty)"
                 print(f"   Row {k}: {val}")
            
            raise HTTPException(status_code=404, detail=f"Order not found (TS: {search_ts_str})")
        
        # --- 1. スプレッドシートの更新 ---
        print("🔹 Updating Spreadsheet...")
        batch_updates = []
        
        new_time_range = f"{payload.startTime} ~ {payload.endTime}"
        
        # 1行目のデータから日付を取得（payloadにない場合のフォールバック）
        current_start_date = target_rows[0][1][6] if len(target_rows[0][1]) > 6 else ""
        primary_date = payload.dates[0] if (payload.dates and len(payload.dates) > 0) else current_start_date

        print(f"   Update Info -> Title: {payload.title}, Date: {primary_date}, Time: {new_time_range}")

        for row_idx, _ in target_rows:
            # C列: Title
            batch_updates.append({'range': f'C{row_idx}', 'values': [[payload.title]]})
            # G列: StartDate, H列: EndDate
            batch_updates.append({'range': f'G{row_idx}:H{row_idx}', 'values': [[primary_date, primary_date]]})
            # K列: TimeRange
            batch_updates.append({'range': f'K{row_idx}', 'values': [[new_time_range]]})
            # L列: SlackThreadTs (シングルクォート付きで小数点保持)
            batch_updates.append({'range': f'L{row_idx}', 'values': [[f"'{search_ts_str}"]]})

        if batch_updates:
            await ws.batch_update(batch_updates, value_input_option="USER_ENTERED")
            print("✅ Spreadsheet updated successfully.")

        # --- 2. Slack通知 ---
        try:
            print("🔹 Sending Slack Notification...")
            dates_text = ', '.join(payload.dates) if payload.dates else '変更なし'
            await slack_client.chat_postMessage(
                channel=SLACK_DEFAULT_CHANNEL,
                thread_ts=payload.slackThreadTs,
                text=f"オーダーが更新されました。\n\n【変更内容】\nタイトル: {payload.title}\n日時: {dates_text}\n時間: {new_time_range}"
            )
            print("✅ Slack Notification sent.")
        except Exception as e:
            print(f"⚠️ Slack update warning: {e}")
        
        # カレンダーイベント更新はフロントエンドで実行
        # （gapiを使用してユーザー認証で更新）
        print("ℹ️ Calendar updates will be handled by frontend using gapi")
        
        print("🎉 Update Process Completed.")
        return {"ok": True, "calendar_event_ids": calendar_event_ids}
    
    except HTTPException as he:
        print(f"❌ HTTPException: {he.detail}")
        raise he
    except Exception as e:
        print(f"❌ Unexpected Error in update_special_order: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/special_order/delete")
async def delete_special_order(payload: SpecialOrderDeletePayload):
    """
    特別オーダーの削除
    同じslackThreadTsを持つレコードを全削除
    """
    if not payload.slackThreadTs:
        raise HTTPException(status_code=400, detail="slackThreadTs required")
    
    try:
        creds = get_creds()
        agcm_local = gspread_asyncio.AsyncioGspreadClientManager(lambda: creds)
        gc = await agcm_local.authorize()
        sh = await gc.open_by_key(SPREADSHEET_ID)
        ws = await sh.worksheet("キャスティングリスト")
        
        # 全データ取得
        all_rows = await ws.get_all_values()
        target_rows = []
        calendar_event_ids = []
        
        # 検索対象のID (完全一致)
        search_ts_str = str(payload.slackThreadTs).strip()
        
        # 検索ループ（完全一致マッチング）
        for i, row in enumerate(all_rows[1:], start=2):
            if len(row) > 11:
                current_ts = str(row[11]).strip()
                if current_ts.startswith("'"):
                    current_ts = current_ts[1:]
                
                if current_ts == search_ts_str:
                    target_rows.append((i, row))
                    # カレンダーイベントID収集（O列）
                    if len(row) > 14 and row[14]:
                        calendar_event_ids.append(row[14])
        
        if not target_rows:
            raise HTTPException(status_code=404, detail=f"Order not found (TS: {payload.slackThreadTs})")
        
        # カレンダーイベント削除はフロントエンドで実行
        print(f"ℹ️ Calendar event deletion will be handled by frontend. Event IDs: {calendar_event_ids}")
        
        # Slack通知
        account_name = target_rows[0][1][1]
        project_name = target_rows[0][1][2]
        await slack_client.chat_postMessage(
            channel=SLACK_DEFAULT_CHANNEL,
            thread_ts=payload.slackThreadTs,
            text=f"オーダーが削除されました。\n【{account_name}】{project_name}"
        )
        
        # シートから削除（後ろから削除して行番号のズレを防ぐ）
        for row_idx, _ in reversed(target_rows):
            await ws.delete_rows(row_idx)
        
        return {"ok": True, "calendar_event_ids": calendar_event_ids}
    
    except Exception as e:
        print(f"Error in delete_special_order: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/order/delete")
async def delete_order(payload: OrderDeletePayload):
    """
    通常オーダー（キャスト単位）の削除
    castingIdで指定されたレコードを削除し、Slack通知を行う
    """
    if not payload.castingId:
        raise HTTPException(status_code=400, detail="castingId required")
    
    try:
        creds = get_creds()
        agcm_local = gspread_asyncio.AsyncioGspreadClientManager(lambda: creds)
        gc = await agcm_local.authorize()
        sh = await gc.open_by_key(SPREADSHEET_ID)
        ws = await sh.worksheet("キャスティングリスト")
        
        # 全データ取得
        all_rows = await ws.get_all_values()
        target_row = None
        target_row_idx = None
        
        # castingIdで検索
        for i, row in enumerate(all_rows[1:], start=2):
            if len(row) > 0 and row[0] == payload.castingId:
                target_row = row
                target_row_idx = i
                break
        
        if not target_row:
            raise HTTPException(status_code=404, detail=f"Order not found (castingId: {payload.castingId})")
        
        # 行データから情報を取得
        account_name = target_row[1] if len(target_row) > 1 else ""
        project_name = target_row[2] if len(target_row) > 2 else ""
        cast_name = target_row[5] if len(target_row) > 5 else ""
        calendar_event_id = target_row[14] if len(target_row) > 14 else ""  # O列
        slack_thread_ts = payload.slackThreadTs or (target_row[11] if len(target_row) > 11 else "")  # L列
        
        # Slack通知（スレッドにリプライ）
        if slack_thread_ts and slack_client:
            try:
                await slack_client.chat_postMessage(
                    channel=SLACK_DEFAULT_CHANNEL,
                    thread_ts=slack_thread_ts,
                    text=f"オーダーが削除されました。\n【{account_name}】{project_name}\nキャスト: {cast_name}"
                )
            except Exception as e:
                print(f"Slack notification error: {e}")
        
        # ステータスを「削除済み」に更新（行は残す）
        await ws.update_acell(f'J{target_row_idx}', '削除済み')
        
        return {
            "ok": True,
            "calendar_event_id": calendar_event_id,
            "deleted_cast": cast_name
        }
    
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error in delete_order: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

# ========== マスターデータシート ==========

async def initialize_master_data_sheet():
    try:
        client = await agcm.authorize()
        ss = await client.open_by_key(SPREADSHEET_ID)
        try:
            ws = await ss.worksheet("マスターデータ")
        except:
            ws = await ss.add_worksheet(title="マスターデータ", rows=1000, cols=16)
        first_cell = await ws.acell('A1')
        if not first_cell.value:
            await ws.update('A1:P1', [[
                "CastingID", "アカウント", "作品名", "役名", "CastID", "キャスト名",
                "開始日", "終了日", "備考", "SlackPermalink", "メイン/サブ",
                "CalenderEventID", "ProjectID", "内部/外部", "金額", "発注書ID"
            ]])
        return True
    except Exception as e:
        print(f"Error init master data: {e}")
        return False

class MasterDataPayload(BaseModel):
    castingId: str
    accountName: str = ""
    projectName: str = ""
    roleName: str = ""
    castId: str = ""
    castName: str = ""
    startDate: str = ""
    endDate: str = ""
    notes: str = ""
    slackPermalink: str = ""
    mainSub: str = "その他"
    calendarEventId: str = ""
    projectId: str = ""
    castType: str = ""
    amount: str = ""
    poId: str = ""

@app.post("/api/master_data/register")
async def register_to_master_data(payload: MasterDataPayload):
    try:
        await initialize_master_data_sheet()
        client = await agcm.authorize()
        ss = await client.open_by_key(SPREADSHEET_ID)
        ws = await ss.worksheet("マスターデータ")
        col_a = await ws.col_values(1)
        row_data = [
            payload.castingId, payload.accountName, payload.projectName, payload.roleName,
            payload.castId, payload.castName, payload.startDate, payload.endDate,
            payload.notes, payload.slackPermalink, payload.mainSub, payload.calendarEventId,
            payload.projectId, payload.castType, payload.amount, payload.poId
        ]
        try:
            row_idx = col_a.index(payload.castingId) + 1
            await ws.update(f'A{row_idx}:P{row_idx}', [row_data])
        except ValueError:
            await ws.append_row(row_data)
        return {"ok": True}
    except Exception as e:
        print(f"Error in master data: {e}")
        raise HTTPException(status_code=500, detail=str(e))
