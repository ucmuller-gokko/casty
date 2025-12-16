import os
import json
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, File, UploadFile, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional, Literal
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
import gspread_asyncio
from google.oauth2.service_account import Credentials
import aiohttp


test

# --- 設定 ---
load_dotenv()

# プロジェクトのルートディレクトリの絶対パスを取得
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# --- 環境変数 ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
SHOOTING_CONTACT_SHEET_ID = os.getenv("SHOOTING_CONTACT_SHEET_ID") # 撮影連絡DBのID

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL")


# カレンダー（内部仮ホールド）
CALENDAR_ID_INTERNAL_HOLD = os.getenv("CALENDAR_ID_INTERNAL_HOLD")

# Slack通知機能追加
SLACK_CHANNEL_TEST = os.getenv("SLACK_CHANNEL_TEST")
SLACK_CHANNEL_TYPE_A = os.getenv("SLACK_CHANNEL_TYPE_A")
SLACK_CHANNEL_TYPE_B = os.getenv("SLACK_CHANNEL_TYPE_B")
SLACK_CHANNEL_TYPE_B = os.getenv("SLACK_CHANNEL_TYPE_B")
SLACK_MENTION_GROUP_ID = os.getenv("SLACK_MENTION_GROUP_ID")

# GAS連携
GAS_URL_NOTION_SYNC = os.getenv("GAS_URL_NOTION_SYNC")

# --- FastAPI アプリ ---
app = FastAPI()

templates = Jinja2Templates(directory=TEMPLATE_DIR)

# static 配信
# app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# --- Slack Client ---
slack_client = AsyncWebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None


# --- Pydantic Models ---

class CastingOrderItem(BaseModel):
    castId: str
    castName: str
    roleName: str
    mainSub: Optional[str] = "その他" # Default to "その他"
    rank: str
    startDate: str
    endDate: str
    status: str
    accountName: str
    projectName: str
    projectId: str
    castPriority: Optional[int] = None


class OrderItem(BaseModel):
    castingId: str
    roleName: str = ""
    castName: str
    rank: int
    note: str = ""
    projectName: str
    slack_user_id: Optional[str] = None


class OrderCreatedPayload(BaseModel):
    accountName: str
    projectName: str
    projectId: str
    dateRanges: List[str]
    orders: List[OrderItem]
    orderType: Literal["pattern_a", "pattern_b", "test"] = "test"
    ccString: Optional[str] = None # Added for CC support
    slackThreadTs: Optional[str] = None # Added for Additional Order support

# ... (existing code) ...



class StatusUpdatePayload(BaseModel):
    castingId: str           # キャスティングID
    newStatus: str           # 変更後ステータス（OK / NG / 条件つきOK など）
    castName: str            # キャスト名
    slackThreadTs: Optional[str] = None
    slackPermalink: Optional[str] = None   # パーマリンク（無くてもOK）
    extraMessage: Optional[str] = None     # 条件つきOKのときの一言
    extraMessage: Optional[str] = None     # 条件つきOKのときの一言
    isInternal: Optional[bool] = False     # 内部/外部フラグ（外部のみSlack連携）
    projectId: Optional[str] = None        # NotionのページID (P列相当)
    mainSub: Optional[str] = "その他"      # キャスト区分 (N列相当)
    orderDetails: Optional[list] = None    # ★追加: W列の構造データ(JSON)を受け取る

    class Config:
        extra = "ignore"

class ShootingContactAddItem(BaseModel):
    castingId: str
    accountName: str
    projectName: str
    roleName: str
    castName: str
    castType: str
    shootDate: str
    note: str

class ShootingContactUpdateItem(BaseModel):
    castingId: str
    status: Optional[str] = None
    inTime: Optional[str] = None
    outTime: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    cost: Optional[str] = None    # ★追加
    makingUrl: Optional[str] = None # O
    postDate: Optional[str] = None # P -> Q
    mainSub: Optional[str] = None # S -> T

def get_creds():
    # .envファイルから読み込んだ認証情報（JSON文字列）を辞書に変換
    creds_json_str = os.getenv("GOOGLE_SHEETS_CREDS_JSON")
    if not creds_json_str:
        raise ValueError("環境変数 'GOOGLE_SHEETS_CREDS_JSON' が設定されていません。")
    
    # スコープを定義
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    
    # eval() ではなく json.loads() を使用する
    try:
        creds_dict = json.loads(creds_json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"環境変数 'GOOGLE_SHEETS_CREDS_JSON' のパースに失敗しました: {e}")

    creds = Credentials.from_service_account_info(
        creds_dict, scopes=scopes
    )
    return creds

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

# --- Slack ヘルパー ---

def pick_channel(order_type: str) -> str:
    """
    オーダー作成時のチャンネル選択
    """
    if order_type == "pattern_a":
        return SLACK_CHANNEL_TYPE_A or SLACK_CHANNEL_TEST or SLACK_DEFAULT_CHANNEL or ""
    if order_type == "pattern_b":
        return SLACK_CHANNEL_TYPE_B or SLACK_CHANNEL_TEST or SLACK_DEFAULT_CHANNEL or ""
    # test / その他
    return SLACK_CHANNEL_TEST or SLACK_DEFAULT_CHANNEL or ""


def pick_status_update_channel(payload: StatusUpdatePayload) -> str:
    """
    ステータス更新時のスレッド投稿先チャンネル。
    いまのところ test チャンネル固定でOK。
    （将来オーダー種別ごとに分ける場合はここを拡張）
    """
    # この関数は新しいエンドポイントでは使われないが、後方互換性のため残す
    return SLACK_DEFAULT_CHANNEL or ""


def build_order_text(payload: OrderCreatedPayload) -> str:
    """
    オーダー作成時の最初の Slack メッセージ本文
    """
    lines: List[str] = []
    if SLACK_MENTION_GROUP_ID:
        lines.append(f"<!subteam^{SLACK_MENTION_GROUP_ID}>")
    
    # CC (Moved to top)
    if payload.ccString:
        lines.append(f"cc: {payload.ccString}")

    lines.append("キャスティングオーダーがありました。")
    lines.append("")

    # 撮影日
    lines.append("`撮影日`")
    for d in payload.dateRanges:
        lines.append(f"・{d}")
    lines.append("")

    # アカウント
    lines.append("`アカウント`")
    lines.append(payload.accountName or "未入力")
    lines.append("")
    lines.append("")

    # Group by Project -> Role
    projects = {}
    for order in payload.orders:
        p_name = order.projectName
        if p_name not in projects:
            projects[p_name] = {}
        
        r_name = order.roleName
        if r_name not in projects[p_name]:
            projects[p_name][r_name] = []
        
        projects[p_name][r_name].append(order)

    for p_name, roles in projects.items():
        lines.append(f"【{p_name}】") # Project Name
        for r_name, candidates in roles.items():
            lines.append(f"  {r_name}") # Role Name
            # Sort by rank
            candidates.sort(key=lambda x: x.rank)
            for cand in candidates:
                lines.append(f"    第{cand.rank}候補：{cand.castName}")
        lines.append("") # Empty line between projects


    # Notionリンク
    lines.append("`Notionリンク`")
    if payload.projectId:
        # プロパティから Notion ページ URL を組み立て
        page_id = payload.projectId.replace("-", "")
        lines.append(f"https://www.notion.so/{page_id}")
    else:
        lines.append("未設定")
    # フッター
    lines.append("")
    lines.append("--------------------------------------------------")
    lines.append("※このメッセージはシステムから自動送信されています。")
    
    return "\n".join(lines).rstrip()


def build_status_update_text(payload: StatusUpdatePayload) -> str:
    """
    スレッドに飛ばすメッセージ本文を組み立てる。
    """
    status = payload.newStatus
    cast_name = payload.castName
    extra_message = payload.extraMessage

    # 追加オーダー専用文面
    if status == "追加オーダー":
        return f"追加オーダーが登録されました。\n{extra_message or ''}".rstrip()

    # 通常 OK / NG / 条件つきOK
    base = f"{cast_name}さん、出演{status}でした。"
    if extra_message:
        return base + "\n" + extra_message
    return base

# --- GAS連携用関数 ---
async def sync_to_notion_via_gas(payload: StatusUpdatePayload):
    """GAS経由でNotionを更新する"""
    if not GAS_URL_NOTION_SYNC:
        print("GAS_URL_NOTION_SYNC is not set.")
        return

    # GASへ送るデータ
    gas_payload = {
        "pageId": payload.projectId,
        "castName": payload.castName,
        "isInternal": payload.isInternal,
        # orderDetailsをJSON文字列化して渡すか、そのまま渡すか（GAS側で調整）
        # ここではGAS側でパースできるようにリストをそのまま渡します（GAS側で要JSON.parseなら文字列化）
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

# --- エンドポイント ---

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/config")
async def get_config():
    """
    フロントエンドで利用する固定IDなどを提供する
    """
    return {
        "calendar_id_internal_hold": CALENDAR_ID_INTERNAL_HOLD,
        "slack_default_channel": SLACK_DEFAULT_CHANNEL,
    }


@app.post("/api/notify/order_created")
async def notify_order_created(
    file: Optional[UploadFile] = File(None),
    payload_str: str = Form(...)
):
    """
    新規オーダー（仮キャスティング）が作成されたことを Slack に通知する
    PDFファイルが添付されている場合はスレッドにアップロードする
    """
    try:
        data = json.loads(payload_str)
        payload = OrderCreatedPayload(**data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payload validation failed: {e}")

    if not SLACK_BOT_TOKEN or not slack_client:
        raise HTTPException(status_code=500, detail="SlackのBOT TOKENが設定されていません。")

    channel = pick_channel(payload.orderType)
    if not channel:
        raise HTTPException(status_code=500, detail="Slack通知先チャンネルが未設定です")

    text = build_order_text(payload)

    try:
        # chat.postMessage APIを呼び出す（スレッド親メッセージ）
        # 追加オーダーの場合はスレッドに返信
        response = await slack_client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=payload.slackThreadTs, # 追加: スレッド指定があれば返信になる
            unfurl_links=False,
            unfurl_media=False,
        )

        ts = response.get("ts")
        if not ts:
            raise HTTPException(status_code=500, detail="Slackメッセージのタイムスタンプが取得できませんでした。")

        # パーマリンクを取得
        permalink = ""
        try:
            perm_res = await slack_client.chat_getPermalink(channel=channel, message_ts=ts)
            if perm_res["ok"]:
                permalink = perm_res["permalink"]
        except Exception as e:
            print(f"Failed to get permalink: {e}")

        # PDFファイルがあればスレッドにアップロード
        upload_error = None
        if file:
            try:
                # ファイルの中身を読み込む
                file_content = await file.read()
                
                # files_upload_v2 を使用
                await slack_client.files_upload_v2(
                    channel=channel,
                    thread_ts=ts,
                    file=file_content,
                    filename=file.filename,
                    title="オーダー添付資料"
                )
            except Exception as e:
                print(f"File upload failed: {e}")
                upload_error = str(e)
                # ファイルアップロード失敗はメインの失敗とはしないがログに残す

        return {"ok": True, "ts": ts, "permalink": permalink, "upload_error": upload_error}

    except Exception as e:
        print(f"An unexpected error occurred in order_created: {e}")
        raise HTTPException(status_code=500, detail="予期せぬエラーが発生しました。")


@app.post("/api/notify/status_update")
async def notify_status_update(
    payload: StatusUpdatePayload,
    background_tasks: BackgroundTasks
):
    """
    ステータス更新時のSlack通知 & Notion同期
    """
    if not SLACK_BOT_TOKEN or not slack_client:
        # Slack設定がなくてもエラーにはしない（運用による）
        print("Slack token not set, skipping notification.")
        # Notion同期は続行したいが、現状はSlack通知APIの一部として実装されている
        # ここではSlack通知スキップのみログ出力して続行
    
    # Notion同期 (OK/決定 の場合)
    if payload.newStatus in ["OK", "決定"]:
        # projectId と castName がある場合のみ実行
        if payload.projectId and payload.castName:
             background_tasks.add_task(sync_to_notion_via_gas, payload)

    if not payload.slackThreadTs:
        # スレッドTSがない場合は通知不要（DB追加のみで終了）
        return JSONResponse(content={"ok": True, "message": "DB append only"})

    # ※ order_created と同じチャンネルにまずは合わせる（テスト環境前提）
    channel = SLACK_CHANNEL_TEST or SLACK_DEFAULT_CHANNEL
    if not channel:
        raise HTTPException(status_code=500, detail="Slack通知先チャンネルが未設定です。")

    # A-3: newStatus が "追加オーダー" の場合の専用メッセージ
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
async def shooting_contact_list():
    try:
        client = await agcm.authorize()
        ss = await client.open_by_key(SHOOTING_CONTACT_SHEET_ID)
        sheet = await ss.worksheet("撮影連絡DB")
        values = await sheet.get_all_values()

        if not values or len(values) < 2:
            return []

        header = values[0]
        rows = values[1:]

        result = []
        for r in rows:
            result.append({
                "castingId": r[0] if len(r) > 0 else "",
                "accountName": r[1] if len(r) > 1 else "",
                "projectName": r[2] if len(r) > 2 else "",
                "notionId": r[3] if len(r) > 3 else "",
                "roleName": r[4] if len(r) > 4 else "", # E
                "castName": r[5] if len(r) > 5 else "", # F
                "castType": r[6] if len(r) > 6 else "", # G
                "shootDate": r[7] if len(r) > 7 else "", # H
                "note": r[8] if len(r) > 8 else "",      # I
                "status": r[9] if len(r) > 9 else "",    # J
                "inTime": r[10] if len(r) > 10 else "",  # K
                "outTime": r[11] if len(r) > 11 else "", # L
                "location": r[12] if len(r) > 12 else "",# M
                "address": r[13] if len(r) > 13 else "", # N
                "makingUrl": r[14] if len(r) > 14 else "",# O
                "cost": r[15] if len(r) > 15 else "",     # P (Cost) ★新規
                "postDate": r[16] if len(r) > 16 else "", # Q (旧P)
                "updatedBy": r[17] if len(r) > 17 else "",# R (旧Q)
                "updatedAt": r[18] if len(r) > 18 else "",# S (旧R)
                "mainSub": r[19] if len(r) > 19 else "その他", # T (旧S)
            })
        return result
    except Exception as e:
        print("shooting_contact_list error:", e)
        raise HTTPException(status_code=500, detail="Shooting contact loading failed")

@app.post("/api/shooting_contact/add")
async def add_shooting_contact(payload: dict):

    # 必須フィールド
    required = [
        "castingId", "account", "projectName", "notionId",
        "roleName", "castName", "castType", "shootDate"
    ]
    for r in required:
        if r not in payload:
            raise HTTPException(status_code=400, detail=f"Missing field: {r}")

    sheet_id = os.getenv("SHOOTING_CONTACT_SHEET_ID")
    if not sheet_id:
        raise HTTPException(status_code=500, detail="SHOOTING_CONTACT_SHEET_ID missing")

    # 行データの構築
    # A: CastingID
    # B: Account
    # C: Project
    # D: NotionID
    # E: Main/Sub (NEW)
    # F: Role
    # G: Cast
    # H: Type
    # I: Date
    # J: Note
    # K: Status
    # L: IN
    # E: RoleName
    # F: CastName
    # G: CastType
    # H: ShootDate
    # I: Note
    # J: Status
    # K: IN
    # L: OUT
    # M: Location (集合場所)
    # N: Address (住所)
    # O: MakingURL
    # P: Cost (新規)
    # Q: PostDate
    # R: UpdatedBy
    # S: UpdatedAt
    # T: Main/Sub (Main/Other)
    
    row = [
        payload["castingId"],        # A (0)
        payload["account"],          # B (1)
        payload["projectName"],      # C (2)
        payload["notionId"],         # D (3)
        payload["roleName"],         # E (4)
        payload["castName"],         # F (5)
        payload["castType"],         # G (6)
        payload["shootDate"],        # H (7)
        payload.get("note", ""),     # I (8)
        "香盤連絡待ち",               # J (9)
        payload.get("inTime", ""),   # K (10)
        payload.get("outTime", ""),  # L (11)
        payload.get("location", ""), # M (12)
        payload.get("address", ""),  # N (13)
        payload.get("makingUrl", ""),# O (14)
        
        payload.get("cost", ""),     # P (15) ★追加（金額）
        
        payload.get("postDate", ""), # Q (16) 旧P
        payload.get("updatedBy", ""),# R (17) 旧Q
        payload.get("updatedAt", ""),# S (18) 旧R
        payload.get("mainSub", "その他"), # T (19) 旧S
    ]

    try:
        client = await agcm.authorize()
        ss = await client.open_by_key(sheet_id)
        sheet = await ss.worksheet("撮影連絡DB")

        await sheet.append_row(row, value_input_option="USER_ENTERED")
        
        return {"ok": True}

    except Exception as e:
        print(f"Error in add_shooting_contact: {e}")
        raise HTTPException(status_code=500, detail=f"append failed: {e}")

    castingId: str
    status: Optional[str] = None
    inTime: Optional[str] = None
    outTime: Optional[str] = None
    location: Optional[str] = None
    address: Optional[str] = None
    makingUrl: Optional[str] = None
    postDate: Optional[str] = None
    mainSub: Optional[str] = None
    cost: Optional[str] = None # ★追加

@app.post("/api/shooting_contact/update")
async def update_shooting_contact_status(payload: ShootingContactUpdateItem):
    sheet_id = os.getenv("SHOOTING_CONTACT_SHEET_ID")
    if not sheet_id:
        raise HTTPException(status_code=500, detail="SHOOTING_CONTACT_SHEET_ID missing")

    try:
        client = await agcm.authorize()
        ss = await client.open_by_key(sheet_id)
        sheet = await ss.worksheet("撮影連絡DB")
        
        col_a = await sheet.col_values(1) # castingId column
        
        try:
            row_idx = col_a.index(payload.castingId) + 1 # 1-based index
        except ValueError:
            raise HTTPException(status_code=404, detail="Casting ID not found")
            
        # Update fields if provided
        # Column mapping:
        # J(9): Status
        # K(10): IN
        # L(11): OUT
        # M(12): Location
        # N(13): Address
        # O(14): MakingURL
        # P(15): PostDate
        # S(18): Main/Sub
        
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
            
        # Q列: PostDate (1つずれた)
        if payload.postDate is not None:
            updates.append({"range": f"Q{row_idx}", "values": [[payload.postDate]]})
            
        # T列: Main/Sub (だいぶ後ろにずれた)
        # ※以前はS列でしたが、Pに挿入されたので T (20列目) になります
        if payload.mainSub is not None:
            updates.append({"range": f"T{row_idx}", "values": [[payload.mainSub]]})
            
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
        # For now, we allow schedule to work, but others might be missing
        raise HTTPException(status_code=501, detail=f"GAS URL for '{type}' is not configured yet.")
    
    try:
        async with aiohttp.ClientSession() as session:
            # GAS uses doGet, so we must use GET
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
    """
    templates/index.html をレンダリングして返す。
    その際に、必要な設定値をテンプレートに渡す。
    """
    return templates.TemplateResponse("index.html", {
        "request": request,
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
        "SPREADSHEET_ID": SPREADSHEET_ID,
        "GOOGLE_API_KEY": GOOGLE_API_KEY,
    })

# サーバーを起動するための記述 (開発用)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
