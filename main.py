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
import logic

# --- 設定 ---
DEFAULT_TIME_LIMIT = 60
HISTORY_FILE = "quiz_history.json"


def main():
    st.set_page_config(page_title="AIクイズボンバー", page_icon="💣", layout="wide")

    logic.load_css()

    st.title("💣 AI クイズボンバー")

    # SecretsからAPIキー読み込み
    api_key = None
    if "GEMINI_API_KEY" in st.secrets:
        api_key = st.secrets["GEMINI_API_KEY"]
    if not api_key:
        st.warning("APIキーが設定されていません。secrets.tomlを確認してください。")
        return

    # セッション初期化
    if 'page' not in st.session_state: st.session_state.page = 'start'
    if 'answers' not in st.session_state: st.session_state.answers = []
    if 'start_time' not in st.session_state: st.session_state.start_time = 0
    if 'current_question' not in st.session_state: st.session_state.current_question = {}
    if 'revealed_hints' not in st.session_state: st.session_state.revealed_hints = []
    if 'feedback_submitted' not in st.session_state: st.session_state.feedback_submitted = False
    # ★ポイント: マスター音量変数をここで初期化（デフォルト0.3）
    if 'master_volume' not in st.session_state: st.session_state.master_volume = 0.3
    if 'result_sound_played' not in st.session_state: 
        st.session_state.result_sound_played = False
    # ★設定保存用
    if 'game_settings' not in st.session_state:
        st.session_state.game_settings = {
            "time_limit": DEFAULT_TIME_LIMIT,
            "genre": "ノンジャンル",
            "difficulty": "中級"
        }    

    # --- 1. スタート画面 ---
    if st.session_state.page == 'start':

        # ★ここが新機能: ゲーム設定エリア
        with st.container():
            st.markdown("### 🛠 ゲーム設定")
            col_s1, col_s2, col_s3 = st.columns(3)
            
            with col_s1:
                genre = st.selectbox(
                    "ジャンル", 
                    ["ノンジャンル", "アニメ・漫画", "歴史・地理", "科学・IT", "グルメ・料理", "スポーツ", "国語・広辞苑",],
                    index=0
                )
            with col_s2:
                diff = st.select_slider(
                    "難易度",
                    options=["初級", "中級", "上級"],
                    value="中級"
                )
            with col_s3:
                tm = st.slider(
                    "制限時間 (秒)",
                    min_value=20, max_value=100, value=60, step=5
                )
            
            # 設定を保存
            st.session_state.game_settings["genre"] = genre
            st.session_state.game_settings["difficulty"] = diff
            st.session_state.game_settings["time_limit"] = tm

        st.write("準備ができたらスタートボタンを押してください。")
        if st.button("ゲームスタート", width="stretch"):
            
            # ★追加: スタート音を鳴らす！
            logic.play_sound("メニューを開く5.mp3")

            # ★追加: リザルト音再生済みフラグをリセット
            st.session_state.result_sound_played = False

            with st.spinner("お題を作成中..."):
                q_data = logic.get_ai_question(
                    api_key,
                    st.session_state.game_settings["genre"], 
                    st.session_state.game_settings["difficulty"])
                
                if q_data:
                    st.session_state.current_question = q_data
                    st.session_state.answers = []
                    st.session_state.revealed_hints = [] # ★リセット
                    st.session_state.result_sound_played = False
                    st.session_state.feedback_submitted = False
                    
                    # 問題文を作成（「お題は、〇〇です」と言わせると自然）
                    speak_text = f"{q_data['question']}"
                    
                    # 音声生成を実行
                    if logic.generate_voice(speak_text, "question_voice.mp3"):
                        # 生成成功したらフラグを立てて、ゲーム画面で再生させる
                        st.session_state.need_play_question = True
                        # ★追加: 読み上げ時間（秒）を計算
                        # 日本語は1文字0.2～0.3秒程度。少し余裕を持たせる
                        st.session_state.speech_duration = len(speak_text) * 0.25 + 1.0
                    else:
                        st.session_state.need_play_question = False
                        st.session_state.speech_duration = 0

                    # ★重要: まだスタート時間は記録しない（読み終わってから記録する）
                    st.session_state.start_time = None    

                    st.session_state.page = 'game'
                    st.rerun()

    # --- 2. ゲーム画面（修正版） ---
    elif st.session_state.page == 'game':
        limit_sec = st.session_state.game_settings["time_limit"]

        # A. 問題読み上げ
        if st.session_state.get('need_play_question', False):
            st.info("🔊 お題を読み上げています...")
            time.sleep(0.5) 
            logic.play_sound("question_voice.mp3")
            st.markdown(f'<div class="question-text">お題：{st.session_state.current_question["question"]}</div>', unsafe_allow_html=True)
            wait_time = st.session_state.get('speech_duration', 3)
            time.sleep(wait_time)
            st.session_state.need_play_question = False
            st.session_state.start_time = time.time()
            st.rerun()

        # B. ゲーム本編
        else:
            if st.session_state.start_time is None:
                st.session_state.start_time = time.time()

            # タイマー更新
            st_autorefresh(interval=1000, limit=None, key="game_timer")
            elapsed = time.time() - st.session_state.start_time
            remaining = limit_sec - elapsed

            if remaining <= 0:
                st.session_state.page = 'exploding'
                st.rerun()

            # ---------------------------------------------------------
            # ★ レイアウト構成
            # 左: 爆弾 (2) / 右: ゲームUI (3)
            # ---------------------------------------------------------
            col_bomb_visual, col_game_ui = st.columns([2, 3])

            # --- 【左】爆弾画像 ---
            with col_bomb_visual:
                if remaining <= 15:
                    bomb_img = "bomb_danger.png"
                    timer_class = "timer-urgent"
                else:
                    bomb_img = "bomb_normal.png"
                    timer_class = "timer-normal"
                
                st.image(bomb_img, width="stretch")
                

            # --- 【右】ゲーム操作エリア ---
            # ★重要: ここからのコードは全て with col_game_ui: のインデントの中に含めること！
            with col_game_ui:
                
                # 1. お題
                st.markdown(f'<div class="question-text">お題：{st.session_state.current_question["question"]}</div>', unsafe_allow_html=True)

                # 1. パーセントを計算 (0~100)
                percent = max(0, min(100, (remaining / limit_sec) * 100))
                
                # 2. 残り時間で色を決める
                if remaining <= 15:
                    bar_color = "#FF4B4B" # 危険：赤色（タイマーと同じ色）
                elif remaining <= (limit_sec / 2):
                    bar_color = "#FFC107" # 注意：黄色（少し焦らせる）
                else:
                    bar_color = "#4CAF50" # 安全：緑色
                
                # 3. HTMLでバーを描画
                # (transitionを入れているので、色が滑らかに変わります)
                st.markdown(f"""
                    <div style="width: 100%; background-color: #333333; border-radius: 5px; height: 20px; margin-bottom: 10px;">
                        <div style="
                            width: {percent}%; 
                            background-color: {bar_color}; 
                            height: 100%; 
                            border-radius: 5px; 
                            transition: width 1s linear, background-color 0.5s;
                        "></div>
                    </div>
                """, unsafe_allow_html=True)

                st.markdown(f'<p class="{timer_class}">{int(remaining)}</p>', unsafe_allow_html=True)


                # 2. 回答スロット
                slots_html = '<div class="slot-container">'
                current_answers = st.session_state.answers
                for i in range(5):
                    if i < len(current_answers):
                        slots_html += f'<div class="answer-slot slot-filled">{current_answers[i]}</div>'
                    else:
                        slots_html += f'<div class="answer-slot">{i + 1}</div>'
                slots_html += '</div>'
                st.markdown(slots_html, unsafe_allow_html=True)

                # 3. ヒント & 入力フォームをまとめるコンテナ
                # （コンテナを使うと要素が散らばりにくくなります）
                with st.container():
                    
                    # --- ヒント行 ---
                    # gap="small" でボタンとテキストの間隔を詰める
                    c_h_btn, c_h_txt = st.columns([1.5, 4.5], gap="small") 
                    
                    with c_h_btn:
                        can_use_hint = (len(st.session_state.revealed_hints) < 5 and remaining > 10)
                        label = "💡 ヒント" if can_use_hint else "💡 ヒント不可"
                        
                        if st.button(label, disabled=not can_use_hint, key="hint_btn", width="stretch"):
                            st.session_state.start_time -= 0
                            all_hints = st.session_state.current_question.get("hints", [])
                            idx = len(st.session_state.revealed_hints)
                            if idx < len(all_hints):
                                st.session_state.revealed_hints.append(all_hints[idx])
                                st.rerun()
                    
                    with c_h_txt:
                        if st.session_state.revealed_hints:
                            # 最後のヒントを表示（CSSで高さを抑える）
                            st.info(f"{st.session_state.revealed_hints[-1]}", icon="🕵️")
                        else:
                            # 空白行を入れてレイアウト崩れを防ぐ
                            st.write("") 

                    # --- 入力フォーム行 ---
                    with st.form(key='ans_form', clear_on_submit=True):
                        c_in, c_sub = st.columns([4, 1], gap="small")
                        with c_in:
                            user_in = st.text_input("回答", key="input_box", label_visibility="collapsed", placeholder="答えを入力...")
                        with c_sub:
                            sub_btn = st.form_submit_button("送信")

                        if sub_btn and user_in:
                            current_rem = limit_sec - (time.time() - st.session_state.start_time)
                            if current_rem <= 0:
                                st.session_state.page = 'exploding'
                                st.rerun()
                            else:
                                st.session_state.answers.append(user_in)
                                if len(st.session_state.answers) >= 5:
                                    st.session_state.page = 'result'
                                st.rerun()

    # =========================================
    # --- ★新規追加: 爆発演出画面 ---
    # =========================================
    elif st.session_state.page == 'exploding':
        current_vol = st.session_state.master_volume
        # ★ここでも音量を渡す
        
        # タイマーの自動更新を止めるために、autorefreshはここでは呼び出さない

        # 画面中央にドカンと表示するためのCSS調整
        st.markdown("""
            <style>
            .explosion-container {
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                height: 80vh; /* 画面の高さの80%くらいを使う */
            }
            .time-up-text {
                font-family: 'Arial Black', sans-serif;
                font-size: 100px;
                color: #FF4B4B;
                text-shadow: 4px 4px 8px #000000;
                margin-bottom: 20px;
            }
            </style>
        """, unsafe_allow_html=True)

        # 画面作成
        st.markdown('<div class="explosion-container">', unsafe_allow_html=True)
        
        # 巨大な「TIME UP!!」文字
        st.markdown('<p class="time-up-text">TIME UP!! 💣</p>', unsafe_allow_html=True)
        
        # 爆発画像を大きく表示（widthでサイズ調整可能）
        st.image("explosion.png", width=1000)
        
        st.markdown('</div>', unsafe_allow_html=True)

        # ★追加: 画面が切り替わった瞬間に爆音！
        logic.play_sound("爆発1.mp3")

        # ★ここで動きを止める！
        # この画面が表示された状態でPython側の処理を2秒止める
        time.sleep(1)

        # 2秒経ったら、自動的に結果画面へ移動
        st.session_state.page = 'result'
        st.rerun()

    # --- 3. 結果画面 ---
    elif st.session_state.page == 'result':
        current_vol = st.session_state.master_volume

        st.subheader("📝 結果発表")
        if 'eval_result' not in st.session_state or st.session_state.get('last_q') != st.session_state.current_question['question']:
            with st.spinner("AI判定中..."):
                res = logic.evaluate_answers(api_key, st.session_state.current_question['question'], st.session_state.answers)
                st.session_state.eval_result = res
                st.session_state.last_q = st.session_state.current_question['question']

        res = st.session_state.eval_result
        if res:
            st.markdown(f"## スコア: {res['score']} / 5")
            st.write(res['comment'])
            for item in res['results']:
                icon = "⭕" if item['is_correct'] else "❌"
                st.write(f"{icon} **{item['answer']}** : {item['reason']}")
            with st.expander("AIの用意した正解例"):
                st.write(st.session_state.current_question['example_answers'])
        
        # ★追加: ここから音の出し分けロジック
            # 「まだリザルト音を鳴らしていない場合」のみ実行する
            if not st.session_state.result_sound_played:
                if res['score'] == 5:
                    # 満点の場合
                    logic.play_sound("歓声と拍手.mp3")
                    st.balloons() # せっかくなので風船も飛ばしましょう！
                elif res['score'] == 4:
                    logic.play_sound("シャキーン3.mp3")
                else:
                    # それ以外の場合
                    logic.play_sound("間抜け7.mp3")
                # ★重要: ここでTrue（鳴らした済み）にする！
                # これにより、次にスライダーを動かしても if not ... の条件に引っかかり、音は鳴らない
                st.session_state.result_sound_played = True    

        st.markdown("---")
        st.subheader("🎓 AIを育てる")
        if not st.session_state.feedback_submitted:
            rating = st.slider("評価", 1, 5, 3, key="rating_slider")
            if st.button("評価を送信"):
                logic.save_feedback(st.session_state.current_question['question'], st.session_state.current_question['example_answers'], rating)
                st.session_state.feedback_submitted = True
                st.success("学習しました！")
                time.sleep(1)
                st.rerun()
        else:
            st.success("✅ 送信済み")

        if st.button("次の問題へ"):
            st.session_state.page = 'start'
            st.rerun()
        
if __name__ == "__main__":
    main()        