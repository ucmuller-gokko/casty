
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

# --- Helpers ---
def get_creds():
    creds_json_str = os.getenv("GOOGLE_SHEETS_CREDS_JSON")
    if not creds_json_str:
        # ローカルファイルフォールバック
        json_path = os.path.join(BASE_DIR, 'google_sheets_creds.json')
        if os.path.exists(json_path):
             scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
             return Credentials.from_service_account_file(json_path, scopes=scopes)
        raise ValueError("環境変数 'GOOGLE_SHEETS_CREDS_JSON' が設定されていません。")
    
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
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
    
    if payload.isAdditionalOrder:
        lines = []
        if SLACK_MENTION_GROUP_ID:
            lines.append(f"<!subteam^{SLACK_MENTION_GROUP_ID}>")
        
        lines.append("追加オーダーのお知らせ")
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
                cast_names = " / ".join([c.castName for c in cands])
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
            
            if hasattr(response, 'data') and isinstance(response.data, dict):
                 files_resp = response.data.get("files", [])
                 if files_resp:
                     shares = files_resp[0].get("shares", {}).get("public", {})
                     if channel in shares:
                         ts = shares[channel][0].get("ts")

        except Exception as e:
            print(f"PDF Upload Failed: {e}")
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
        except Exception as e:
            print(f"Text Message Failed: {e}")
            raise HTTPException(status_code=500, detail="Slack送信失敗")

    if ts:
        try:
            perm = await slack_client.chat_getPermalink(channel=channel, message_ts=ts)
            permalink = perm.get("permalink", "")
        except:
            pass

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

        for cid in payload.castIds:
            cid_str = str(cid).strip()
            cast = cast_map.get(cid_str, {})
            cast_name = cast.get("name") or "不明"
            cast_email = cast.get("email") or ""
            cast_type = cast.get("type") or "外部"
            is_internal_cast = (cast_type == "内部")
            status = "仮キャスティング" if is_internal_cast else "決定"

            slack_id = cast.get("slack_id") or ""
            mention = f"<@{slack_id}>" if slack_id else cast_name
            
            orderer_email_key = payload.ordererEmail.strip().lower()
            cc_slack_id = email_to_slack_map.get(orderer_email_key)
            cc_mention = f"<@{cc_slack_id}>" if cc_slack_id else payload.ordererEmail

            dates_str = ", ".join(payload.dates).replace("-", "/")
            time_range = f"{payload.startTime} ~ {payload.endTime}"
            
            msg = f"{mention} \nCC: {cc_mention}\n\n"
            msg += f"【{account_name}】\n"
            msg += f"`タイトル`\n{payload.title}\n"
            msg += f"`日時`\n{dates_str}\n"
            msg += f"`時間`\n{time_range}"

            ts = None
            permalink = ""
            try:
                resp = await slack_client.chat_postMessage(
                    channel=SLACK_DEFAULT_CHANNEL,
                    text=msg
                )
                ts = resp.get("ts")
                if ts:
                    perm = await slack_client.chat_getPermalink(channel=SLACK_DEFAULT_CHANNEL, message_ts=ts)
                    permalink = perm.get("permalink", "")
            except Exception as e:
                print(f"Slack error: {e}")

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
                    f"{time_range}",        # K
                    ts,                     # L
                    permalink,              # M
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
                        "time_range": time_range,
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
            
        # ★P列: Cost (新規)
        if payload.cost is not None:
            updates.append({"range": f"P{row_idx}", "values": [[payload.cost]]})
            
        if payload.postDate is not None:
            updates.append({"range": f"Q{row_idx}", "values": [[payload.postDate]]})
            
        if payload.mainSub is not None:
            updates.append({"range": f"T{row_idx}", "values": [[payload.mainSub]]})

        if payload.poUuid is not None:
            updates.append({"range": f"U{row_idx}", "values": [[payload.poUuid]]})

        # Update Timestamp (S列)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates.append({"range": f"S{row_idx}", "values": [[now_str]]})
            
        if updates:
            await sheet.batch_update(updates)
        
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
