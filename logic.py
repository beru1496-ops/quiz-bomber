#使用モジュール
import streamlit as st
from google import genai
from google.genai import types
import time
import json
import re
import os
import random
import base64
from streamlit_autorefresh import st_autorefresh
from tenacity import retry, stop_after_attempt, wait_fixed
from gtts import gTTS
import uuid
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import datetime

# --- 設定 ---
DEFAULT_TIME_LIMIT = 60
HISTORY_FILE = "quiz_history.json"
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

#AIとテキストでやり取り
def clean_json_text(text):
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()

def connect_to_sheet():
    """Secretsから認証情報を読み込んでシートに接続"""
    try:
        # st.secrets["gcp_service_account"] の辞書データをそのまま使う
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
        client = gspread.authorize(creds)
        
        # シート名 または URL で開く
        # 名前で開く場合: 分かりやすい名前にしておいてください
        sheet = client.open("quiz_feedback").sheet1 
        return sheet
    except Exception as e:
        st.error(f"スプレッドシート接続エラー: {e}")
        return None

#蓄積された過去のデータからAIにいくつか渡すため抽出
def load_examples_by_rating():
    """履歴から良い例(5,4)と悪い例(1,2)を抽出して返す"""
    if not os.path.exists(HISTORY_FILE):
        return [], []
    
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
            
            # 良い例: 5は優先、4も少し混ぜる
            good_qs = [h['question'] for h in history if h.get('rating', 0) == 5]
            okay_qs = [h['question'] for h in history if h.get('rating', 0) == 4]
            # 悪い例: 1と2
            bad_qs = [h['question'] for h in history if h.get('rating', 0) <= 2]
            
            # 良い例リスト構築
            final_good = good_qs + random.sample(okay_qs, min(len(okay_qs), 2))
            return final_good, bad_qs
    except:
        return [], []
    
#問題データと回答例、フィードバックを保存
def save_feedback(question, example_answers, rating):
    """
    結果をGoogleスプレッドシートのみに保存する
    （ローカルのJSON保存機能は削除済み）
    """
    try:
        # 1. シートに接続
        sheet = connect_to_sheet()
        
        # シートが見つからなかった場合（接続エラーなど）
        if not sheet:
            st.error("エラー: スプレッドシートに接続できませんでした。")
            return False

        # 2. 現在時刻を取得
        now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 3. 保存するデータを作成
        # [日時, お題, 評価] の順
        row = [now_str, question, rating]
        
        # 4. 書き込み実行
        sheet.append_row(row)
        
        # 成功ログ（Manage appの黒い画面で見れる用）
        print(f"Spreadsheet saved: {row}")
        
        return True

    except Exception as e:
        # エラーが起きたら画面に表示して知らせる
        st.error(f"スプレッドシート書き込み中にエラーが発生しました: {e}")
        return False

#googleの自動音声を再生
def generate_voice(text, filename="question_voice.mp3"):
    """
    Googleのサーバーを使って音声を生成する（オンライン対応）
    """
    try:
        # lang='ja' で日本語を指定
        tts = gTTS(text=text, lang='ja')
        tts.save(filename)
        return True
    except Exception as e:
        st.error(f"音声生成エラー: {e}")
        return False
        

#AIを使っての問題と正解例の生成
@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
def get_ai_question(api_key, genre, difficulty):
    """履歴を考慮してAIにお題を作らせる"""
    try:
        # 1. クライアントの初期化 (ここでAPIキーを設定)
        client = genai.Client(api_key=api_key)
        
        # --- ここで履歴をロードしてプロンプトに組み込む ---
        good_examples, bad_examples = load_examples_by_rating()
        
        examples_text = ""
        if good_examples:
            picks = random.sample(good_examples, min(len(good_examples), 5))
            examples_text += "【ユーザーが好むお題の傾向（これらを参考にしてください）】:\n" + "\n".join([f"- {p}" for p in picks]) + "\n"
        
        if bad_examples:
            picks = random.sample(bad_examples, min(len(bad_examples), 3))
            examples_text += "【ユーザーが嫌うお題の傾向（これらは避けてください）】:\n" + "\n".join([f"- {p}" for p in picks]) + "\n"
        
        # 難易度に応じた指示
        difficulty_instruction = ""
        if difficulty == "初級":
            difficulty_instruction = "小学生でもわかる簡単な、答えやすい内容にしてください。"
        elif difficulty == "上級":
            difficulty_instruction = "知識が必要な、少しマニアックでひねった内容にしてください。"
        else:
            difficulty_instruction = "一般的で、誰でも思いつくが5つ出すのは少し焦る程度の内容にしてください。"

        # ジャンル指示
        genre_instruction = f"ジャンルは「{genre}」に限定してください。" if genre != "ノンジャンル" else "ジャンルは問いません（バラエティ豊かに）。"

        prompt = f"""
        クイズ番組のような「〇〇なものを5つ答えろ」形式のお題を1つ作成してください。
        また、その正解例と正解例一つ一つに対する「ヒント（頭文字や文字数など正解を推測するためのもの）」も作成してください。
        
        条件:
        1. 「嫌うお題」の要素は避け、「好むお題」に近い雰囲気で作ること。
        2. {genre_instruction}
        3. 難易度: {difficulty}。{difficulty_instruction}
        4. {examples_text}
        5. 以下のJSON形式のみで出力すること。挨拶不要。
        {{
            "question": "お題テキスト",
            "items": [
                {{"answer": "正解例1", "hint": "正解例1のヒント"}},
                {{"answer": "正解例2", "hint": "正解例2のヒント"}},
                {{"answer": "正解例3", "hint": "正解例3のヒント"}},
                {{"answer": "正解例4", "hint": "正解例4のヒント"}},
                {{"answer": "正解例5", "hint": "正解例5のヒント"}}
            ]
        }}
        """
        response = client.models.generate_content(
            model='gemini-2.5-flash', # 最新モデル指定
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", # ★重要: これで確実にJSONになる
                temperature=0.7 # 創造性の調整もここに書く
            )
        )
        data = json.loads(clean_json_text(response.text))
        
        # ★重要: 既存のロジックを壊さないようデータを整形する
        # itemsの中から、answerだけを抜き出して従来の example_answers に入れる
        question_data = {
            "question": data["question"],
            "example_answers": [item["answer"] for item in data["items"]],
            "hints": [item["hint"] for item in data["items"]] # ヒントリストを別途保持
        }
        return question_data
    

    except Exception as e:
        st.error(f"お題生成エラー: {e}")
        return None

#ユーザーの入力した答えを判定
@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
def evaluate_answers(api_key, question, user_answers):
    """回答判定"""
    try:
        client = genai.Client(api_key=api_key)
        
        prompt = f"""
        お題: {question}
        回答リスト: {user_answers}
        
        上記回答の正誤判定を行い、以下のJSON形式で返してください。
        {{
            "score": 正解数(整数),
            "results": [
                {{"answer": "回答1", "is_correct": true, "reason": "OK"}},
                {{"answer": "回答2", "is_correct": false, "reason": "NG理由"}}
            ],
            "comment": "短い総評"
        }}
        """
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json" # JSON強制
            )
        )
        return json.loads(clean_json_text(response.text))
    except Exception as e:
        st.error(f"判定エラー: {e}")
        return None

#問題文、効果音を再生する
def play_sound(file_path, visible=False):
    """
    指定した音声ファイルを再生する。
    音量調節機能は削除し、確実に再生されることを優先。
    visible=True の場合のみ、プレイヤーを表示する（手動で音量変更は可能）。
    """
    try:
        with open(file_path, "rb") as f:
            data = f.read()
            b64 = base64.b64encode(data).decode()
            
            # ユニークID生成
            sound_id = f"audio_{uuid.uuid4()}"

            # プレイヤーの表示・非表示設定
            display_style = "width: 300px;" if visible else "display:none;"
            controls_attr = "controls" if visible else ""
            
            # シンプルなHTML埋め込み（余計なJSは排除）
            md = f"""
                <audio id="{sound_id}" {controls_attr} autoplay style="{display_style}">
                    <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
                </audio>
                """
            st.markdown(md, unsafe_allow_html=True)
            
    except FileNotFoundError:
        pass # ファイルがない場合は無視 

def load_css():
    with open("style.css", "r",encoding='utf-8') as f:

        st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)                   

