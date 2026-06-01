import os
import re
import html
import time
import json
import logging
import socket
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET
from pydantic import BaseModel, Field
from typing import Literal
from google import genai
from google.genai import types

# ネットワークフリーズ防止グローバルタイムアウト
socket.setdefaulttimeout(30)

# ==========================================
# 1. ログ・フォルダ初期設定
# ==========================================
os.makedirs("logs", exist_ok=True)
os.makedirs("articles", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("books", exist_ok=True)

logging.basicConfig(
    filename="logs/app.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

MAX_ARTICLES_LIMIT = 50
MAX_HISTORY_LIMIT = 5000
TEMPLATE_VERSION = "9.0.0"  # 4号店 究極インテグレーション仕様

# ==========================================
# 2. Pydanticスキーマ定義（生存辞典・ロードマップ仕様）
# ==========================================
class PersonaBenefit(BaseModel):
    persona_name: str = Field(description="この用語に直結する関連性の高いターゲット。15文字以内。")
    benefit: str = Field(description="その立場の読者にとって、この知識が今日からどう役に立ち、どうラクになるか。150〜200文字程度。")

class FAQItem(BaseModel):
    question: str = Field(description="想定されるよくある質問。25文字以内。")
    answer: str = Field(description="質問に対する客観的で簡潔な回答。70文字以内。")

class ArticleOutputSchema(BaseModel):
    title: str = Field(description="不安・欲望・優越を刺激する35文字以内のタイトル。記事タイプに最適なSEOキーワードを必ず含めること。")
    search_intent: str = Field(description="読者の検索意図（'KNOW' または 'DO' ）。")
    
    # 3層ピラー・テーマクラスターインフラ
    category: str = Field(description="大カテゴリ。例：『AIツール』『IT技術・インフラ』など。10文字以内。")
    topic_cluster: str = Field(description="親テーマ（クラスター）名。例：『ChatGPT活用群』など。15文字以内。")
    cluster_slug: str = Field(description="親テーマのスラグ。英数字ハイフンのみ。例:『chatgpt-guide』。")
    difficulty_level: Literal['beginner', 'intermediate', 'advanced'] = Field(description="用語の難易度自律判定。")
    estimated_read_time: int = Field(description="想定読了時間（分）。3、5、8などの半角数値。")
    article_type: Literal['definition', 'comparison', 'application', 'troubleshooting', 'monetization'] = Field(description="記事のタイプ。")
    
    # 30秒結論
    quick_definition: str = Field(description="この用語は一言で何か？ 体言止めで45文字以内。")
    quick_target: str = Field(description="どのような人向けのものか？ 25文字以内。")
    quick_features: list[str] = Field(description="主な特徴やできること。必ず3つの短いリスト。")
    quick_importance: str = Field(description="重要度判定。15文字以内。")
    
    # 概念理解のグラデーション
    one_word_summary: str = Field(description="一言でいうと（20文字以内）。")
    explain_level_1: str = Field(description="5歳児比喩。体言止め45文字以内。")
    explain_level_2: str = Field(description="簡単にいうと？（中学生向け）。200〜300文字程度。")
    explain_level_3: str = Field(description="つまりどういうこと？（社会人向け）。300〜450文字程度。")
    
    persona_benefits: list[PersonaBenefit] = Field(description="関連ターゲットとそのメリット。2〜3つ生成。")
    faq_list: list[FAQItem] = Field(description="想定されるFAQ。必ず3つ生成。")
    charo_insight: str = Field(description="編集長cocoroの眼。200文字程度。")
    today_mission: str = Field(description="具体的アクション。100文字程度。")
    slug: str = Field(description="半角英数字とハイフンのみのスラグ。")

# ==========================================
# 3. 各種ユーティリティ
# ==========================================
def sanitize_slug(raw_slug: str) -> str:
    slug = re.sub(r'[^a-z0-9\-]', '', raw_slug.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        slug = f"explain-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return slug[:80]

def get_strategy_context(article_text: str) -> str:
    strategy_path = os.path.join("data", "strategy_master.json")
    if not os.path.exists(strategy_path):
        return ""
    try:
        with open(strategy_path, "r", encoding="utf-8") as f:
            strategy_data = json.load(f)
        
        matched_info = []
        text_lower = article_text.lower()
        
        for key, value in strategy_data.items():
            trigger = value.get("keyword_trigger", "").lower()
            if trigger and (trigger in text_lower or key.lower() in text_lower):
                keywords_str = ", ".join(value.get("seo_keywords", []))
                links_str = "\n".join([f"- [{l['title']}]({l['url']})" for l in value.get("trust_links", [])])
                matched_info.append(f"【生存戦略カテゴリ: {key}】\n■SEOキーワード: {keywords_str}\n■一次情報リンク:\n{links_str}")
        
        if matched_info:
            return "\n\n=== 突合された公的一次情報 ＆ 対策キーワード ===\n" + "\n\n".join(matched_info)
    except Exception as e:
        logging.error(f"戦略マスター突合失敗: {e}")
    return ""

# 履歴管理
HISTORY_FILE = "logs/history.json"
def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"履歴読み込み失敗: {e}")
    return []

def save_history(history: list):
    try:
        trimmed = history[-MAX_HISTORY_LIMIT:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"履歴保存失敗: {e}")

# 五十音/アルファベット頭文字判別ヘルパー
def get_index_char(title: str) -> str:
    if not title:
        return "#"
    first_char = title[0].upper()
    if re.match(r'[A-Z0-9]', first_char):
        return first_char
    
    # 簡易五十音マッピング（ひらがな・カタカナ対応）
    hira = "あかさたなはまやらわ"
    kana = "アカサタナハマヤラワ"
    
    # unicodeの平仮名判定
    if '\u3040' <= first_char <= '\u309f' or '\u30a0' <= first_char <= '\u30ff':
        code = ord(first_char)
        if '\u30a0' <= first_char <= '\u30ff':  # カタカナを平仮名に変換
            code -= 96
        char_converted = chr(code)
        
        # 行ごとの分類
        for i, (h, k) in enumerate(zip(hira[:-1], hira[1:])):
            if h <= char_converted < k:
                return hira[i]
        return "わ"
    return "#"

# ==========================================
# 4. RSS取得・スクレイピング
# ==========================================
def fetch_rss_feed(rss_url: str) -> list:
    articles = []
    try:
        req = urllib.request.Request(
            rss_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            root = ET.fromstring(response.read())
        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else ""
            link = item.find('link').text if item.find('link') is not None else ""
            desc = item.find('description').text if item.find('description') is not None else ""
            articles.append({"title": title, "link": link, "description": desc})
    except Exception as e:
        logging.error(f"RSS取得失敗: {e}")
    return articles

def fetch_full_article_text(url: str) -> str:
    try:
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
        for tag in ['script', 'style', 'header', 'footer', 'nav']:
            html_content = re.sub(f'<{tag}[\\s\\S]*?>[\\s\\S]*?</{tag}>', '', html_content)
        html_content = re.sub(r'</?(p|div|h1|h2|h3|h4|li|br)[^>]*>', '\n', html_content)
        text = re.sub(r'<[^>]+>', ' ', html_content)
        text = html.unescape(text)
        return re.sub(r'\n\s*\n+', '\n', text).strip()
    except Exception as e:
        logging.warning(f"全文スクレイピング失敗: {e}")
        return ""

# ==========================================
# 5. レイアウト結合エンジン（Combined JSON-LD @graph & ロードマップソート）
# ==========================================
def build_page(body_template_path, title, date_iso, date_ja, source_url, source_name, replacements, output_path, is_article=False, slug="", art=None, all_articles=None) -> bool:
    try:
        if not os.path.exists("layout.html") or not os.path.exists(body_template_path):
            logging.error(f"テンプレート欠損: {body_template_path}")
            return False

        with open("layout.html", "r", encoding="utf-8") as f:
            layout_content = f.read()
        with open(body_template_path, "r", encoding="utf-8") as f:
            body_content = f.read()

        combined_content = layout_content.replace("{{BODY_CONTENT}}", body_content)

        # 関連記事・ロードマップ動的ソート
        if is_article and art and all_articles:
            related_html = ""
            cluster_articles = []
            backup_articles = []

            for _, art_data in all_articles:
                if art_data["slug"] == slug:
                    continue
                if art_data.get("cluster_slug") == art.get("cluster_slug"):
                    cluster_articles.append(art_data)
                elif art_data.get("category") == art.get("category"):
                    backup_articles.append(art_data)

            # 難易度連動ソートロジック
            curr_diff = art.get("difficulty_level", "beginner")
            if curr_diff == "intermediate":
                sort_order = ["intermediate", "advanced", "beginner"]
            elif curr_diff == "advanced":
                sort_order = ["advanced", "intermediate", "beginner"]
            else:
                sort_order = ["beginner", "intermediate", "advanced"]

            cluster_articles.sort(key=lambda x: sort_order.index(x.get("difficulty_level", "beginner")) if x.get("difficulty_level", "beginner") in sort_order else 9)
            backup_articles.sort(key=lambda x: sort_order.index(x.get("difficulty_level", "beginner")) if x.get("difficulty_level", "beginner") in sort_order else 9)

            final_related = (cluster_articles + backup_articles)[:3]

            for r_art in final_related:
                diff_ja = {"beginner": "初級者", "intermediate": "中級者", "advanced": "上級者"}.get(r_art.get("difficulty_level", "beginner"), "基本")
                related_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span class="difficulty-tag" style="border: 1px solid var(--border-color); padding: 1px 6px; border-radius: 4px; font-weight:700;">{diff_ja}向け</span>
                        <span>{html.escape(r_art.get('topic_cluster', '現代用語'))}</span>
                    </div>
                    <h3>{html.escape(r_art['title'])}</h3>
                    <p>{html.escape(r_art['one_word_summary'])}</p>
                    <a href="articles/{r_art['slug']}.html">解説を体系的に読む &rarr;</a>
                </article>
                """
            replacements["{{RELATED_ARTICLES_HTML}}"] = related_html

        # 置換の安全実行（HTMLエスケープ処理）
        raw_keys = ["{{RELATED_ARTICLES_HTML}}", "{{WEEKLY_BOOK_BANNER}}", "{{ARTICLES_GRID}}", "{{BOOK_CONTENT}}", "{{PERSONA_BENEFITS_HTML}}", "{{FAQ_LIST_HTML}}", "{{INDEX_NAVIGATION_HTML}}"]
        for placeholder, value in replacements.items():
            if placeholder in raw_keys:
                combined_content = combined_content.replace(placeholder, value)
            else:
                combined_content = combined_content.replace(placeholder, html.escape(str(value)))

        # 構造化データとパスの動的切替
        if is_article:
            combined_content = combined_content.replace("{{CSS_PATH}}", "/style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "/script.js")
            
            # Combined JSON-LD @graph の自動生成
            ld_json_graph = {
                "@context": "https://schema.org",
                "@graph": [
                    {
                        "@type": "Article",
                        "@id": f"https://explain.pray-power-is-god-and-cocoro.com/articles/{slug}.html#article",
                        "headline": title,
                        "datePublished": date_iso,
                        "author": {"@type": "Person", "name": "cocoro"},
                        "description": art.get("one_word_summary", title) if art else title,
                        "mainEntityOfPage": source_url
                    },
                    {
                        "@type": "BreadcrumbList",
                        "@id": f"https://explain.pray-power-is-god-and-cocoro.com/articles/{slug}.html#breadcrumb",
                        "itemListElement": [
                            {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://explain.pray-power-is-god-and-cocoro.com/"},
                            {"@type": "ListItem", "position": 2, "name": art.get("category", "生存辞典") if art else "生存辞典", "item": "https://explain.pray-power-is-god-and-cocoro.com/"},
                            {"@type": "ListItem", "position": 3, "name": art.get("topic_cluster", "クラスター") if art else "クラスター", "item": "https://explain.pray-power-is-god-and-cocoro.com/"}
                        ]
                    }
                ]
            }
            # FAQページの統合
            if art and art.get("faq_list"):
                ld_json_graph["@graph"].append({
                    "@type": "FAQPage",
                    "@id": f"https://explain.pray-power-is-god-and-cocoro.com/articles/{slug}.html#faq",
                    "mainEntity": [
                        {
                            "@type": "Question",
                            "name": item["question"],
                            "acceptedAnswer": {"@type": "Answer", "text": item["answer"]}
                        } for item in art["faq_list"]
                    ]
                })

            serialized_json = json.dumps(ld_json_graph, ensure_ascii=False, indent=2)
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", f'<script type="application/ld+json">\n{serialized_json}\n</script>')
        else:
            combined_content = combined_content.replace("{{CSS_PATH}}", "style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "script.js")
            structured_data = """
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@type": "WebSite",
              "name": "AI Frontier Explain",
              "url": "https://explain.pray-power-is-god-and-cocoro.com/"
            }
            </script>
            """
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", structured_data)

        # 平文置換
        combined_content = combined_content.replace("{{TITLE}}", html.escape(title))
        combined_content = combined_content.replace("{{DATE_ISO}}", date_iso)
        combined_content = combined_content.replace("{{DATE_JA}}", date_ja)
        combined_content = combined_content.replace("{{SOURCE_URL}}", html.escape(source_url))
        combined_content = combined_content.replace("{{SOURCE_NAME}}", html.escape(source_name))

        if art:
            combined_content = combined_content.replace("{{CATEGORY}}", html.escape(art.get("category", "辞典")))
            combined_content = combined_content.replace("{{TOPIC_CLUSTER}}", html.escape(art.get("topic_cluster", "クラスター")))
            combined_content = combined_content.replace("{{ARTICLE_TYPE}}", html.escape(art.get("article_type", "definition").upper()))
            combined_content = combined_content.replace("{{DIFFICULTY_LEVEL}}", html.escape(art.get("difficulty_level", "beginner").upper()))
            combined_content = combined_content.replace("{{ESTIMATED_READ_TIME}}", html.escape(str(art.get("estimated_read_time", 3))))

        tmp_out = output_path + ".tmp"
        with open(tmp_out, "w", encoding="utf-8") as f:
            f.write(combined_content)
        os.replace(tmp_out, output_path)
        return True
    except Exception as e:
        logging.error(f"ビルド失敗 ({output_path}): {e}")
        return False

# ==========================================
# 6. コア：生存辞典解説記事のAI自動生成
# ==========================================
def run_article_generator(source_text: str, source_url: str, source_name: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.error("GEMINI_API_KEY が設定されていません。")
        return ""

    safe_text = source_text[:12000]
    strategy_context = get_strategy_context(safe_text)

    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    # 極限まで読者本位にチューニングされたシステムプロンプト
    prompt = f"""
    あなたは、激変するAI・IT社会において、読者に「人生を生き抜く智慧としての用語辞典・生存マニュアル」を授ける最高峰の戦略編集長です。
    提供された【情報素材】と【公的一次情報・生存戦略マスター情報】をマージし、以下の【ルール】に沿って全自動執筆してください。

    【ルール】
    - 単なる辞書的な用語解説ではなく、「これを知ることで、読者の人生、キャリア、副業、マインドセットがどう有利に好転するか」に焦点をあててください。
    - タイトルは35文字以内。突合されたSEOキーワードを必ず1つ以上自然に含めること。
    - categoryは『AI・ITツール』『心理・思考法』『副業・ビジネス』『自動化・実務』などの大ピラー名を設定してください（10文字以内）。
    - 難易度（difficulty_level）は 'beginner', 'intermediate', 'advanced' から自律選択してください。
    - explain_level_1（5歳児比喩）は、読者が一瞬で直感的にイメージを掴めるよう、100%日常のモノや体験（例：おもちゃ、電車、学校、お店など）に例えて体言止め45文字以内で記述してください。
    - explain_level_2（簡単にいうと？）は、専門用語を使わずに中学生が読んでも100%理解できるように200〜300文字程度で論理的に記述してください。
    - explain_level_3（つまりどういうこと？）は、現代の経済構造やビジネス現場のリアルな課題と関連づけ、社会人向けに300〜450文字程度で論理的に詳細記述してください。
    - persona_benefitsには、異なるターゲット層（エンジニア、ブログ初心者、会社員、個人開発者など）を2〜3つ自律生成してメリットを記述してください。
    - faq_listには、読者がその用語に関して抱く可能性の高い疑問を3つQ&A形式で論理的に記述してください。
    - slugは半角英数字とハイフンのみ。

    【情報素材】
    {safe_text}
    {strategy_context}
    """

    MAX_RETRIES = 3
    response_text = ""
    for attempt in range(MAX_RETRIES):
        try:
            logging.info(f"Gemini API 呼び出し中 (試行 {attempt + 1}/{MAX_RETRIES})...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ArticleOutputSchema,
                    http_options=types.HttpOptions(timeout=60000)
                )
            )
            if response and response.text:
                response_text = response.text.strip()
                break
            else:
                raise ValueError("空のレスポンスを受信しました。")
        except Exception as e:
            wait = 2 ** attempt
            logging.warning(f"API呼び出し一時失敗（試行 {attempt + 1}）: {e}。リトライします...")
            time.sleep(wait)
    else:
        logging.error("リトライ超過。生成を断念します。")
        return ""

    response_text = re.sub(r"^```json\s*|\s*```$", "", response_text, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(response_text)
        validated = ArticleOutputSchema(**data)
    except Exception as e:
        logging.error(f"Pydantic検証失敗: {e}\nレスポンス: {response_text}")
        return ""

    art = validated.model_dump()
    slug = sanitize_slug(art["slug"])

    # 中身のJSONデータを保存
    art["source_url"] = source_url
    art["source_name"] = source_name
    art["template_version"] = TEMPLATE_VERSION
    output_json_path = os.path.join("data", f"{slug}.json")
    
    try:
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(art, f, ensure_ascii=False, indent=2)
        logging.info(f"記事生成 ＆ JSONデータ保存成功: {slug}")
        return slug
    except Exception as e:
        logging.error(f"JSON保存失敗: {e}")
        return ""

# ==========================================
# 7. サイト内完結型プチ生存書籍パブリッシャー
# ==========================================
def get_weekly_book_banner_html() -> str:
    if not os.path.exists("books"):
        return ""
    book_files = [f for f in os.listdir("books") if f.endswith(".html")]
    if not book_files:
        return ""
    book_files.sort(key=lambda x: os.path.getmtime(os.path.join("books", x)), reverse=True)
    latest_book = book_files[0]
    book_slug = os.path.splitext(latest_book)[0]
    display_title = f"{datetime.now().strftime('%Y年%m月')} 最新号：現代生存のための知恵・統合マスターバイブル"
    
    return f"""
    <section class="weekly-book-banner fade-element" style="margin-bottom: 40px;">
        <div style="background: linear-gradient(135deg, #0f172a, #1e293b); color: white; padding: 30px; border-radius: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); text-align: center;">
            <span style="background: rgba(255, 255, 255, 0.15); padding: 4px 12px; border-radius: 999px; font-size: 0.8rem; font-weight: 800; letter-spacing: 0.05em;">🆕 SURVIVAL WEEKLY BOOK 配信中</span>
            <h2 style="font-size: 1.6rem; font-weight: 800; margin: 15px 0 10px; color: white;">{display_title}</h2>
            <p style="font-size: 0.95rem; color: rgba(255, 255, 255, 0.85); max-width: 500px; margin: 0 auto 20px; line-height: 1.6;">蓄積された専門用語・概念を体系的なロードマップとして1冊に再編集。混沌とした時代において、自分の意思決定基準を確立するための特別書籍です。</p>
            <a href="books/{book_slug}.html" class="toggle-button" style="background: white; color: #1e293b; border: none; font-weight: 800; margin-top: 0; display: inline-block; padding: 12px 24px; border-radius: 8px; text-decoration: none;">電子書籍を読む（無料） &rarr;</a>
        </div>
    </section>
    """

def generate_weekly_book():
    logging.info("=== 自動週刊書籍パブリッシング開始 ===")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return

    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
        if len(json_files) < 5:
            logging.info("記事数不足により書籍生成を保留します（最低5記事以上必要）。")
            return

        combined_materials = []
        for j_file in json_files[:15]:
            try:
                with open(os.path.join("data", j_file), "r", encoding="utf-8") as f:
                    art = json.load(f)
                combined_materials.append(f"【生存用語】: {art['title']}\n【本質】: {art['one_word_summary']}\n【解説】: {art['explain_level_3']}\n【インサイト】: {art['charo_insight']}")
            except Exception as e:
                continue

        if not combined_materials:
            return

        client = genai.Client(api_key=api_key)
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        materials_text = "\n\n---\n\n".join(combined_materials)

        prompt = f"""
        あなたは、激変するAI・情報社会を生き抜く全人類の生存戦略羅針盤となる、圧倒的な編集知性を持つプロライターです。
        以下の【生存概念・用語集の断片データ】を美しく統合し、現代の生存マニュアルとしての1万文字規模の「体系的電子書籍」を執筆してください。

        【執筆構成案】
        第1章：地殻変動する社会における生存マインドセット
        第2章：現代知識のロードマップと賢い技術選択
        第3章：中学生でも納得できる「これからの生き方」の核心比喩
        第4章：ココロの平穏とメンタル調和の具体策
        第5章：明日から即座にアクションを起こすべき実践行動マップ

        【ルール】
        - markdownの装飾（```html や ``` など）はいっさい出力せず、直接h3, p, strong, blockquote等のHTMLタグだけを純粋に出力してください。

        【データ素材】
        {materials_text}
        """

        book_html_content = ""
        for attempt in range(MAX_RETRIES := 3):
            try:
                logging.info(f"Gemini API 書籍執筆中 (試行 {attempt + 1}/{MAX_RETRIES})...")
                response = client.models.generate_content(model=model_name, contents=prompt)
                if response and response.text:
                    book_html_content = response.text.strip()
                    break
                else:
                    raise ValueError("レスポンスが空です。")
            except Exception as e:
                time.sleep(2 ** attempt)
        else:
            return

        book_html_content = re.sub(r"^```html\s*|\s*```$", "", book_html_content, flags=re.IGNORECASE).strip()
        book_title = f"{datetime.now().strftime('%Y年%m月')}号：AI時代の生存戦略 ＆ 知識統合マスターバイブル"
        book_slug = f"weekly-survival-book-{datetime.now().strftime('%Y-%m-w%W')}"
        
        build_page(
            body_template_path="template_book.html",
            title=book_title,
            date_iso=datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            date_ja=datetime.now().strftime("%Y年%m月%d日"),
            source_url="#",
            source_name="AI Frontier Life 編集部",
            replacements={"{{BOOK_CONTENT}}": book_html_content},
            output_path=os.path.join("books", f"{book_slug}.html"),
            is_article=True,
            slug=book_slug
        )
    except Exception as e:
        logging.error(f"電子書籍生成エラー: {e}")

# ==========================================
# 8. 再ビルド（SSGコンパイル、索引ナビ、ローテーション）
# ==========================================
def rebuild_index_and_rotate_storage():
    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
        all_articles = []

        for j_file in json_files:
            path = os.path.join("data", j_file)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    article_data = json.load(f)
                mtime = os.path.getmtime(path)
                all_articles.append((mtime, article_data))
            except Exception as e:
                logging.error(f"JSON読込失敗: {e}")

        # 新しい順にソート
        all_articles.sort(key=lambda x: x[0], reverse=True)

        # 古いデータのローテーション自動削除
        if len(all_articles) > MAX_ARTICLES_LIMIT:
            logging.info("データ上限超過のため古いデータを自動ローテーション削除します。")
            to_delete = all_articles[MAX_ARTICLES_LIMIT:]
            all_articles = all_articles[:MAX_ARTICLES_LIMIT]
            for _, d_art in to_delete:
                d_slug = sanitize_slug(d_art["slug"])
                for p in [os.path.join("articles", f"{d_slug}.html"), os.path.join("data", f"{d_slug}.json")]:
                    if os.path.exists(p):
                        os.remove(p)

        if not all_articles:
            logging.info("再ビルド対象データが空です。")
            return

        # 1. すべての個別用語記事の再コンパイル（学習ロードマップ連携）
        for mtime, art in all_articles:
            a_slug = sanitize_slug(art["slug"])
            a_date_ja = datetime.fromtimestamp(mtime).strftime("%Y年%m月%d日 %H:%M")
            a_date_iso = datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S+09:00")
            
            # 立場別メリットHTMLのビルド
            benefits_html = ""
            for p_ben in art.get("persona_benefits", []):
                benefits_html += f"""
                <div class="level-box" style="background: var(--card-bg); border: 1px solid var(--border-color); padding: 25px; border-radius: 16px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.01);">
                    <h4 style="font-size: 1.1rem; font-weight: 800; margin-top: 0; margin-bottom: 12px; color: var(--accent-color);">👤 {html.escape(p_ben['persona_name'])}にとっての価値</h4>
                    <p style="font-size: 1rem; line-height: 1.8; color: var(--text-color); margin: 0; text-align: justify;">{html.escape(p_ben['benefit'])}</p>
                </div>
                """
            
            # FAQ HTMLのビルド
            faq_html = ""
            for faq_item in art.get("faq_list", []):
                faq_html += f"""
                <div style="margin-bottom: 20px; border: 1px solid var(--border-color); border-radius: 12px; padding: 18px; background: var(--bg-accent);">
                    <strong style="display: block; font-size: 1rem; color: var(--text-color); margin-bottom: 8px;">💡 Q. {html.escape(faq_item['question'])}</strong>
                    <span style="display: block; font-size: 0.95rem; line-height: 1.6; color: var(--text-muted);">A. {html.escape(faq_item['answer'])}</span>
                </div>
                """

            build_page(
                body_template_path="template_article.html",
                title=art["title"],
                date_iso=a_date_iso,
                date_ja=a_date_ja,
                source_url=art.get("source_url", "#"),
                source_name=art.get("source_name", "ソース"),
                replacements={
                    "{{QUICK_DEFINITION}}": art["quick_definition"],
                    "{{QUICK_TARGET}}": art["quick_target"],
                    "{{QUICK_IMPORTANCE}}": art["quick_importance"],
                    "{{ONE_WORD_SUMMARY}}": art["one_word_summary"],
                    "{{EXPLAIN_LEVEL_1}}": art["explain_level_1"],
                    "{{EXPLAIN_LEVEL_2}}": art["explain_level_2"],
                    "{{EXPLAIN_LEVEL_3}}": art["explain_level_3"],
                    "{{CHARO_INSIGHT}}": art["charo_insight"],
                    "{{TODAY_MISSION}}": art["today_mission"],
                    "{{SEARCH_INTENT}}": art.get("search_intent", "KNOW"),
                    "{{PERSONA_BENEFITS_HTML}}": benefits_html,
                    "{{FAQ_LIST_HTML}}": faq_html
                },
                output_path=os.path.join("articles", f"{a_slug}.html"),
                is_article=True,
                slug=a_slug,
                art=art,
                all_articles=all_articles
            )

        # 2. 五十音順・アルファベット索引ナビゲーション生成
        index_map = {}
        for _, art in all_articles:
            head = get_index_char(art["title"])
            if head not in index_map:
                index_map[head] = []
            index_map[head].append(art)

        # ナビゲーションバーHTMLのビルド
        sorted_heads = sorted(index_map.keys(), key=lambda x: ord(x))
        nav_html = " | ".join([f'<a href="#index-{h}" style="font-weight: 800; text-decoration: underline;">{h}</a>' for h in sorted_heads])

        # 3. インデックス、アーカイブのビルド
        _, hero_art = all_articles[0]
        hero_date_ja = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        hero_date_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

        # 記事一覧グリッドの構築
        articles_grid_html = ""
        for _, art in all_articles[1:]:
            a_title = html.escape(art["title"])
            a_slug = sanitize_slug(art["slug"])
            diff_ja = {"beginner": "初心者", "intermediate": "中級者", "advanced": "上級者"}.get(art.get("difficulty_level", "beginner"), "基本")
            articles_grid_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span class="intent-badge">{html.escape(art.get('search_intent', 'KNOW'))}</span>
                        <span class="difficulty-tag" style="border: 1px solid var(--border-color); padding: 1px 6px; border-radius: 4px;">{diff_ja}向け</span>
                        <span>Latest Release</span>
                    </div>
                    <h3>{a_title}</h3>
                    <p>{html.escape(art['one_word_summary'])}</p>
                    <a href="articles/{a_slug}.html">解説を読む &rarr;</a>
                </article>
            """

        weekly_book_banner = get_weekly_book_banner_html()

        # index.html の出力
        build_page(
            body_template_path="template_index.html",
            title=hero_art["title"],
            date_iso=hero_date_iso,
            date_ja=hero_date_ja,
            source_url=hero_art.get("source_url", "#"),
            source_name=hero_art.get("source_name", "ソース"),
            replacements={
                "{{QUICK_DEFINITION}}": hero_art["quick_definition"],
                "{{ONE_WORD_SUMMARY}}": hero_art["one_word_summary"],
                "{{EXPLAIN_LEVEL_2}}": hero_art["explain_level_2"],
                "{{EXPLAIN_LEVEL_3}}": hero_art["explain_level_3"],
                "{{CHARO_INSIGHT}}": hero_art["charo_insight"],
                "{{TODAY_MISSION}}": hero_art["today_mission"],
                "{{SEARCH_INTENT}}": hero_art.get("search_intent", "KNOW"),
                "{{WEEKLY_BOOK_BANNER}}": weekly_book_banner,
                "{{ARTICLES_GRID}}": articles_grid_html
            },
            output_path="index.html",
            is_article=False
        )

        # archive.htmlのビルド（索引に基づく体系的配置）
        archive_html = ""
        for head in sorted_heads:
            archive_html += f'<h3 id="index-{head}" style="font-size: 1.4rem; margin-top: 40px; margin-bottom: 20px; border-bottom: 2px solid var(--accent-color); padding-bottom: 5px;">📍 {head}</h3>'
            archive_html += '<div class="articles-grid">'
            for art in index_map[head]:
                a_title = html.escape(art["title"])
                a_slug = sanitize_slug(art["slug"])
                diff_ja = {"beginner": "初心者", "intermediate": "中級者", "advanced": "上級者"}.get(art.get("difficulty_level", "beginner"), "基本")
                archive_html += f"""
                    <article class="article-card fade-element">
                        <div class="article-meta">
                            <span class="difficulty-tag" style="border: 1px solid var(--border-color); padding: 1px 6px; border-radius: 4px;">{diff_ja}向け</span>
                            <span>{html.escape(art.get('topic_cluster', '生存スキル'))}</span>
                        </div>
                        <h3>{a_title}</h3>
                        <p>{html.escape(art['one_word_summary'])}</p>
                        <a href="articles/{a_slug}.html">解説を読む &rarr;</a>
                    </article>
                """
            archive_html += '</div>'

        build_page(
            body_template_path="template_archive.html",
            title="現代生存用語 検索・索引アーカイブ",
            date_iso=hero_date_iso,
            date_ja=hero_date_ja,
            source_url="#",
            source_name="アーカイブ",
            replacements={
                "{{INDEX_NAVIGATION_HTML}}": nav_html,
                "{{ARTICLES_GRID}}": archive_html
            },
            output_path="archive.html",
            is_article=False
        )
        print("✅ 4号店生存辞典：再ビルド・索引生成が正常に完了しました！")
    except Exception as e:
        logging.error(f"再ビルドエラー: {e}")

# ==========================================
# 9. オーケストレーター（メイン処理）
# ==========================================
def main():
    # 4号店用：実務・生存戦略に直結する海外ITマクロ・ロードマップ情報源を自動検知
    RSS_FEEDS = [
        {"url": "https://www.reutersagency.com/feed/?best-topics=tech&post_type=best", "name": "Reuters Tech Macro"},
        {"url": "https://www.cnbc.com/id/19854910/device/rss/rss.html", "name": "CNBC Tech Strategy"}
    ]

    logging.info("--- 4号店：自動巡回・生存辞典生成プロセス開始 ---")
    history = load_history()
    processed_urls = {h["url"] for h in history if "url" in h}
    new_article_created = False
    
    data_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
    
    # 初期シードデータ（デモ自動立ち上げ用）
    if not data_files:
        if os.environ.get("ALLOW_DEMO_SEED", "true").lower() == "true":
            mock_text = "Model Context Protocol (MCP) is an open standard that enables developers to build secure, bi-directional connections between AI models and their data sources. This simplifies integration and accelerates AI agent engineering."
            slug = run_article_generator(mock_text, "https://www.reutersagency.com/feed/", "Reuters MCP Guide Seed")
            if slug:
                new_article_created = True

    MAX_PROCESS_PER_RUN = 1
    processed_count = 0

    for feed in RSS_FEEDS:
        fetched = fetch_rss_feed(feed["url"])
        if not fetched:
            continue
        if processed_count >= MAX_PROCESS_PER_RUN:
            break

        for item in fetched:
            if processed_count >= MAX_PROCESS_PER_RUN:
                break
            if item["link"] in processed_urls:
                continue

            if not item["description"] or len(item["description"]) < 100:
                history.append({"url": item["link"], "processed_at": datetime.now().isoformat(), "status": "skipped"})
                processed_urls.add(item["link"])
                continue

            logging.info(f"未処理シード検知: {item['title']}")
            full_text = fetch_full_article_text(item["link"])
            if not full_text:
                full_text = item["description"]

            slug = run_article_generator(full_text, item["link"], feed["name"])
            if slug:
                new_article_created = True
                history.append({"url": item["link"], "processed_at": datetime.now().isoformat(), "status": "published"})
                processed_urls.add(item["link"])
                processed_count += 1
                
    if new_article_created:
        generate_weekly_book()
        rebuild_index_and_rotate_storage()
        save_history(history)
    else:
        rebuild_index_and_rotate_storage()

if __name__ == '__main__':
    main()
