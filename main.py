import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# --- 設定 ---
load_dotenv()

# プロジェクトのルートディレクトリの絶対パスを取得
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static") # staticフォルダも念のため定義

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# --- FastAPIアプリケーション ---
app = FastAPI()

# 静的ファイルとテンプレートの設定
# app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static") # 現状は使わないが将来のために残す
templates = Jinja2Templates(directory=TEMPLATE_DIR)

# --- エンドポイント ---

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

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
        "GOOGLE_API_KEY": GOOGLE_API_KEY
    })

# サーバーを起動するための記述 (開発用)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
