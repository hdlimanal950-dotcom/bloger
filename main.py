#!/usr/bin/env python3
"""
She Cooks Bakes AI v5.2.0 FINAL
✅ FIXED: Concise content (no long recipes)
✅ FIXED: Professional static image (no random images)
✅ FIXED: Fallback redirects to landing page
✅ FIXED: Quality validation for AI responses
✅ All previous features maintained
"""
import os, sys, json, time, random, logging, sqlite3, requests, threading, shutil
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from contextlib import contextmanager
from collections import deque
from waitress import serve
from flask import Flask, jsonify, render_template_string, request
import google.generativeai as genai
import pytumblr
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from logging.handlers import RotatingFileHandler

class Config:
    APP_NAME = "She Cooks Bakes AI"
    VERSION = "5.2.0"
    ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
    FLASK_HOST = "0.0.0.0"
    FLASK_PORT = int(os.getenv("PORT", 10000))
    MIN_DELAY_SECONDS = 3600
    MAX_DELAY_SECONDS = 7200
    HUMAN_DELAY_MIN = 30
    HUMAN_DELAY_MAX = 90
    DB_PATH = "she_cooks_bakes.db"
    DB_BACKUP_DIR = "backups"
    DB_BACKUP_INTERVAL = 86400
    DB_MAX_BACKUPS = 7
    TOPIC_CACHE_SIZE = 10
    GEMINI_MAX_RETRIES = 3
    GEMINI_RETRY_DELAY = 5
    TUMBLR_MAX_RETRIES = 3
    TUMBLR_RETRY_DELAY = 3
    GEMINI_RATE_LIMIT_PAUSE = 60
    SELF_PING_ENABLED = True
    SELF_PING_INTERVAL = 840
    BLOG_URL = "shecooksandbakes.tumblr.com"
    BLOG_WEBSITE = "https://shecooksbakes.blogspot.com/p/header-footer-nav.html"
    LOG_FILE = "she_cooks_bakes.log"
    LOG_MAX_BYTES = 5 * 1024 * 1024
    LOG_BACKUP_COUNT = 3
    METRICS_ENABLED = True
    GLOBAL_HASHTAGS = ["Food", "Recipe", "Cooking", "Chef", "Yummy", "InstaFood", "Delicious", "Foodie", "HomeCooking", "FoodLover"]
    PROFESSIONAL_IMAGE = "https://images.unsplash.com/photo-1556909114-f6e7ad7d3136?w=1200&h=800&fit=crop&q=80"

def setup_logging():
    logger = logging.getLogger(Config.APP_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    file_handler = RotatingFileHandler(Config.LOG_FILE, maxBytes=Config.LOG_MAX_BYTES, backupCount=Config.LOG_BACKUP_COUNT, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s'))
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger

logger = setup_logging()

class PerformanceMetrics:
    def __init__(self):
        self.enabled = Config.METRICS_ENABLED
        self.metrics = {'posts_published': 0, 'posts_failed': 0, 'api_calls': 0, 'api_errors': 0, 'avg_publish_time': 0, 'last_publish_time': None}
        self.publish_times = deque(maxlen=50)
    def record_publish_success(self, duration: float):
        if not self.enabled: return
        self.metrics['posts_published'] += 1
        self.metrics['last_publish_time'] = datetime.now().isoformat()
        self.publish_times.append(duration)
        if self.publish_times: self.metrics['avg_publish_time'] = sum(self.publish_times) / len(self.publish_times)
    def record_publish_failure(self):
        if self.enabled: self.metrics['posts_failed'] += 1
    def record_api_call(self, success: bool = True):
        if not self.enabled: return
        self.metrics['api_calls'] += 1
        if not success: self.metrics['api_errors'] += 1
    def get_metrics(self): return self.metrics.copy()
    def get_success_rate(self):
        total = self.metrics['posts_published'] + self.metrics['posts_failed']
        return (self.metrics['posts_published'] / total * 100) if total > 0 else 100.0

metrics = PerformanceMetrics()

class DatabaseManager:
    def __init__(self, db_path: str = Config.DB_PATH):
        self.db_path = db_path
        self.backup_dir = Config.DB_BACKUP_DIR
        self._lock = threading.Lock()
        self.init_database()
        self._setup_backups()
    @contextmanager
    def get_connection(self):
        conn = None
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA synchronous=NORMAL')
                conn.execute('PRAGMA cache_size=-64000')
                yield conn
        finally:
            if conn: conn.close()
    def init_database(self):
        with self.get_connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT UNIQUE, topic TEXT NOT NULL, title TEXT NOT NULL, image_url TEXT, tags TEXT, published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT \'success\', duration_seconds REAL)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at DESC)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)')
            conn.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
            conn.execute('CREATE TABLE IF NOT EXISTS errors (id INTEGER PRIMARY KEY AUTOINCREMENT, error_type TEXT, error_message TEXT, stack_trace TEXT, occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
            conn.execute('CREATE TABLE IF NOT EXISTS topic_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT UNIQUE NOT NULL, last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
            conn.execute('CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, metric_name TEXT NOT NULL, metric_value REAL NOT NULL, recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
            conn.commit()
        logger.info("✅ Database initialized")
    def _setup_backups(self):
        if not os.path.exists(self.backup_dir): os.makedirs(self.backup_dir)
        def backup_loop():
            while True:
                time.sleep(Config.DB_BACKUP_INTERVAL)
                self.create_backup()
        threading.Thread(target=backup_loop, daemon=True).start()
        logger.info("✅ Auto-backup started")
    def create_backup(self):
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = os.path.join(self.backup_dir, f'backup_{timestamp}.db')
            shutil.copy2(self.db_path, backup_path)
            logger.info(f"💾 Backup: {backup_path}")
            self._cleanup_old_backups()
            return True
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return False
    def _cleanup_old_backups(self):
        try:
            backups = sorted([f for f in os.listdir(self.backup_dir) if f.startswith('backup_')])
            while len(backups) > Config.DB_MAX_BACKUPS:
                os.remove(os.path.join(self.backup_dir, backups.pop(0)))
        except: pass
    def save_post(self, post_data: Dict, duration: float = 0):
        try:
            with self.get_connection() as conn:
                conn.execute('INSERT INTO posts (post_id, topic, title, image_url, tags, status, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)', (post_data.get('post_id'), post_data.get('topic'), post_data.get('title'), post_data.get('image_url'), json.dumps(post_data.get('tags', [])), 'success' if post_data.get('success') else 'failed', duration))
                conn.commit()
            return True
        except: return False
    def update_state(self, key: str, value: str):
        try:
            with self.get_connection() as conn:
                conn.execute('INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP', (key, value))
                conn.commit()
        except: pass
    def get_state(self, key: str, default: Optional[str] = None):
        try:
            with self.get_connection() as conn:
                result = conn.execute('SELECT value FROM state WHERE key = ?', (key,)).fetchone()
                return result[0] if result else default
        except: return default
    def log_error(self, error_type: str, error_message: str, stack_trace: str = ""):
        try:
            with self.get_connection() as conn:
                conn.execute('INSERT INTO errors (error_type, error_message, stack_trace) VALUES (?, ?, ?)', (error_type, error_message, stack_trace))
                conn.commit()
        except: pass
    def get_post_count(self):
        try:
            with self.get_connection() as conn:
                return conn.execute('SELECT COUNT(*) FROM posts WHERE status = "success"').fetchone()[0]
        except: return 0
    def get_last_post_time(self):
        try:
            with self.get_connection() as conn:
                result = conn.execute('SELECT published_at FROM posts WHERE status = "success" ORDER BY published_at DESC LIMIT 1').fetchone()
                return datetime.fromisoformat(result[0]) if result else None
        except: return None
    def add_to_topic_cache(self, topic: str):
        try:
            with self.get_connection() as conn:
                conn.execute('INSERT OR REPLACE INTO topic_cache (topic, last_used) VALUES (?, CURRENT_TIMESTAMP)', (topic,))
                conn.commit()
        except: pass
    def get_recent_topics(self, limit: int = 10):
        try:
            with self.get_connection() as conn:
                return [row[0] for row in conn.execute('SELECT topic FROM topic_cache ORDER BY last_used DESC LIMIT ?', (limit,)).fetchall()]
        except: return []
    def save_metric(self, name: str, value: float):
        try:
            with self.get_connection() as conn:
                conn.execute('INSERT INTO metrics (metric_name, metric_value) VALUES (?, ?)', (name, value))
                conn.commit()
        except: pass

class EnvironmentValidator:
    REQUIRED_VARS = ['TUMBLR_CONSUMER_KEY', 'TUMBLR_CONSUMER_SECRET', 'TUMBLR_OAUTH_TOKEN', 'TUMBLR_OAUTH_SECRET', 'GEMINI_API_KEY']
    @staticmethod
    def validate():
        missing = [var for var in EnvironmentValidator.REQUIRED_VARS if not os.getenv(var)]
        if missing:
            error_msg = f"Missing: {', '.join(missing)}"
            logger.critical(error_msg)
            raise EnvironmentError(error_msg)
        logger.info("✅ Environment validated")
        return True

class APIClients:
    def __init__(self):
        self.tumblr_client = pytumblr.TumblrRestClient(consumer_key=os.getenv('TUMBLR_CONSUMER_KEY'), consumer_secret=os.getenv('TUMBLR_CONSUMER_SECRET'), oauth_token=os.getenv('TUMBLR_OAUTH_TOKEN'), oauth_secret=os.getenv('TUMBLR_OAUTH_SECRET'))
        genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
        self.gemini_model = genai.GenerativeModel('gemini-pro')
        logger.info("✅ API clients initialized (Google Gemini)")
    def retry_with_smart_backoff(self, func, operation_name: str, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                metrics.record_api_call(success=True)
                return func()
            except Exception as e:
                metrics.record_api_call(success=False)
                error_str = str(e).lower()
                if 'quota' in error_str or 'rate' in error_str or 'limit' in error_str:
                    logger.warning(f"⚠️ Rate limit: {operation_name}")
                    if attempt < max_retries - 1:
                        time.sleep(Config.GEMINI_RATE_LIMIT_PAUSE)
                    else:
                        raise
                elif 'connection' in error_str or 'network' in error_str:
                    logger.warning(f"⚠️ Connection error: {operation_name}")
                    if attempt < max_retries - 1:
                        time.sleep(Config.GEMINI_RETRY_DELAY * (2 ** attempt))
                    else:
                        raise
                else:
                    logger.error(f"❌ API error: {operation_name}")
                    if attempt < max_retries - 1:
                        time.sleep(Config.GEMINI_RETRY_DELAY * (2 ** attempt))
                    else:
                        raise

class ContentEngine:
    def __init__(self, api_clients: APIClients, db_manager: DatabaseManager):
        self.clients = api_clients
        self.db = db_manager
        self.blog_url = Config.BLOG_URL
        self.base_tags = ["Baking", "DessertRecipes", "CookingTips", "HomeBaking", "SweetTreats", "RecipeIdeas", "FoodBlog", "DeliciousDesserts", "BakingLove"]
    
    def generate_recipe_topic(self):
        topics = ["Chocolate Lava Cake", "French Macarons", "Artisan Bread", "Italian Tiramisu", "Red Velvet Cupcakes", "Vegan Cookies", "Professional Cheesecake", "Baklava", "Matcha Desserts", "American Pancakes", "Belgian Truffles", "French Croissants", "New York Cheesecake", "Italian Cannoli", "Spanish Churros", "British Scones", "Black Forest Cake", "Apple Strudel", "Custard Tarts", "Chocolate Fondue", "Cinnamon Rolls", "Lemon Pie", "Carrot Cake", "Brownies", "Banana Bread"]
        recent = self.db.get_recent_topics(Config.TOPIC_CACHE_SIZE)
        available = [t for t in topics if t not in recent]
        if not available: 
            logger.info("🔄 Topic reset")
            available = topics
        topic = random.choice(available)
        self.db.add_to_topic_cache(topic)
        return topic
    
    def generate_ai_content_with_hashtags(self, topic: str):
        def _generate():
            prompt = f'''Create a SHORT, ENGAGING blog post about "{topic}".

Requirements:
1. Title: Catchy 5-8 words
2. Introduction: 2-3 engaging sentences
3. Key Highlights: 3-4 bullet points about why this recipe is special
4. Call-to-action: Invite readers to visit the full recipe
5. Two custom hashtags (no # symbol)

Format EXACTLY as JSON:
{{
    "title": "...",
    "introduction": "...",
    "highlights": ["Point 1", "Point 2", "Point 3"],
    "cta": "...",
    "custom_hashtags": ["Tag1", "Tag2"]
}}

IMPORTANT: Keep it SHORT and PROMOTIONAL. NO full recipe, NO ingredients list.'''
            
            response = self.clients.gemini_model.generate_content(prompt)
            content = response.text.strip()
            
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0].strip()
            elif '```' in content:
                content = content.split('```')[1].split('```')[0].strip()
            
            try:
                data = json.loads(content)
                required = ['title', 'introduction', 'highlights', 'cta']
                for field in required:
                    if field not in data:
                        raise ValueError(f"Missing: {field}")
                
                if 'custom_hashtags' not in data or len(data['custom_hashtags']) != 2:
                    data['custom_hashtags'] = self._extract_hashtags(topic)
                
                logger.info(f"✅ AI content validated successfully")
                return data
                
            except:
                logger.warning("⚠️ JSON parse failed")
                raise ValueError("Invalid AI response")
        
        try: 
            return self.clients.retry_with_smart_backoff(_generate, "Gemini AI", Config.GEMINI_MAX_RETRIES)
        except:
            logger.error("❌ AI failed, using landing page fallback")
            return self._fallback_landing_page(topic)
    
    def _fallback_landing_page(self, topic: str):
        """Fallback: Redirect to landing page"""
        return {
            "title": f"Discover Our {topic} Recipe!",
            "introduction": f"We're excited to share our professional {topic} recipe with you!",
            "highlights": [
                f"Complete step-by-step {topic} guide",
                "Professional baking tips and tricks",
                "Beautiful photos and detailed instructions"
            ],
            "cta": f"Visit our blog to get the full {topic} recipe with all ingredients and steps!",
            "custom_hashtags": self._extract_hashtags(topic),
            "is_fallback": True
        }
    
    def _extract_hashtags(self, topic: str):
        words = topic.replace('-', ' ').split()
        if len(words) >= 2:
            return [''.join(words[:2]), topic.replace(' ', '') + 'Recipe']
        return [topic.replace(' ', ''), 'Baking']
    
    def get_professional_image(self):
        """Returns professional static image"""
        timestamp = int(time.time())
        return f"{Config.PROFESSIONAL_IMAGE}&t={timestamp}"
    
    def generate_final_hashtags(self, custom_hashtags: List[str]):
        final = []
        for tag in custom_hashtags[:2]:
            clean = tag.replace('#', '').replace(' ', '')
            if clean:
                final.append(clean)
        final.extend(random.sample(Config.GLOBAL_HASHTAGS, 8))
        final.extend(self.base_tags[:5])
        return list(dict.fromkeys(final))[:15]
    
    def format_post_content(self, ai_content: Dict):
        """Format SHORT promotional content"""
        is_fallback = ai_content.get('is_fallback', False)
        highlights = "".join([f'<li style="margin: 10px 0;">{h}</li>' for h in ai_content.get('highlights', [])])
        
        cta_style = "background: linear-gradient(135deg, #ff6b6b 0%, #ee5a6f 100%);" if is_fallback else "background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);"
        
        return f'''<div style="font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.8; max-width: 700px; margin: 0 auto; padding: 20px;">
<h1 style="color: #2c3e50; font-size: 28px; margin-bottom: 15px; border-bottom: 3px solid #e74c3c; padding-bottom: 10px;">
{ai_content["title"]}
</h1>

<p style="font-size: 18px; color: #34495e; background: #f8f9fa; padding: 20px; border-radius: 8px; border-left: 4px solid #3498db; margin: 25px 0;">
{ai_content["introduction"]}
</p>

<h3 style="color: #e74c3c; margin-top: 30px; font-size: 22px;">✨ Why You'll Love This:</h3>
<ul style="color: #555; font-size: 16px; line-height: 2; background: #fff9e6; padding: 25px 25px 25px 45px; border-radius: 8px;">
{highlights}
</ul>

<div style="{cta_style} color: white; padding: 30px; border-radius: 12px; margin-top: 30px; text-align: center; box-shadow: 0 4px 15px rgba(0,0,0,0.2);">
<h2 style="color: white; margin: 0 0 15px 0; font-size: 24px;">🎯 {ai_content["cta"]}</h2>
<a href="{Config.BLOG_WEBSITE}" target="_blank" style="display: inline-block; background: white; color: #2c3e50; padding: 15px 40px; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 18px; margin-top: 10px; transition: transform 0.3s;">
📖 Get Full Recipe Now →
</a>
<p style="margin: 15px 0 0 0; font-size: 14px; opacity: 0.9;">Complete ingredients, steps & pro tips</p>
</div>

<hr style="margin: 30px 0; border: none; border-top: 2px dashed #ddd;">
<p style="font-size: 13px; color: #95a5a6; text-align: center;">
Posted by She Cooks Bakes • {datetime.now().strftime("%B %d, %Y")}
</p>
</div>'''

class PublishingManager:
    def __init__(self, content_engine: ContentEngine, db_manager: DatabaseManager):
        self.engine = content_engine
        self.db = db_manager
    
    def human_delay(self, min_s=None, max_s=None):
        time.sleep(random.randint(min_s or Config.HUMAN_DELAY_MIN, max_s or Config.HUMAN_DELAY_MAX))
    
    def publish_post(self):
        start = time.time()
        try:
            topic = self.engine.generate_recipe_topic()
            logger.info(f"🍰 Topic: {topic}")
            
            logger.info("📝 Generating content...")
            ai_content = self.engine.generate_ai_content_with_hashtags(topic)
            
            if ai_content.get('is_fallback'):
                logger.warning("⚠️ Using fallback content (landing page)")
            
            logger.info(f"🏷️ Hashtags: {ai_content.get('custom_hashtags', [])}")
            self.human_delay(20, 40)
            
            logger.info("🎨 Using professional image...")
            image_url = self.engine.get_professional_image()
            self.human_delay(30, 50)
            
            final_tags = self.engine.generate_final_hashtags(ai_content.get('custom_hashtags', []))
            content = self.engine.format_post_content(ai_content)
            
            logger.info("🚀 Publishing to Tumblr...")
            response = self.engine.clients.retry_with_smart_backoff(
                lambda: self.engine.clients.tumblr_client.create_photo(
                    blogname=self.engine.blog_url,
                    state="published",
                    tags=final_tags,
                    caption=content,
                    source=image_url,
                    format="html"
                ), 
                "Tumblr", 
                Config.TUMBLR_MAX_RETRIES
            )
            
            post_id = response.get('id', 'unknown')
            duration = time.time() - start
            
            post_data = {
                "success": True,
                "post_id": str(post_id),
                "topic": topic,
                "title": ai_content['title'],
                "image_url": image_url,
                "tags": final_tags
            }
            
            self.db.save_post(post_data, duration)
            self.db.update_state('last_post_time', datetime.now().isoformat())
            self.db.update_state('total_posts', str(self.db.get_post_count()))
            metrics.record_publish_success(duration)
            self.db.save_metric('publish_duration', duration)
            
            logger.info(f"✅ Published in {duration:.1f}s! ID: {post_id}")
            return post_data
            
        except Exception as e:
            duration = time.time() - start
            logger.error(f"❌ Failed after {duration:.1f}s: {e}")
            import traceback
            self.db.log_error('publish_failed', str(e), traceback.format_exc())
            metrics.record_publish_failure()
            return {"success": False, "error": str(e), "topic": topic if 'topic' in locals() else 'unknown'}
    
    def calculate_next_post_time(self):
        last = self.db.get_last_post_time()
        if last:
            elapsed = (datetime.now() - last).total_seconds()
            delay = int(Config.MIN_DELAY_SECONDS - elapsed) if elapsed < Config.MIN_DELAY_SECONDS else random.randint(Config.MIN_DELAY_SECONDS, Config.MAX_DELAY_SECONDS)
        else:
            delay = 60
        return delay, datetime.now() + timedelta(seconds=delay)

class PostScheduler:
    def __init__(self, publisher: PublishingManager, db_manager: DatabaseManager):
        self.publisher = publisher
        self.db = db_manager
        jobstores = {'default': MemoryJobStore()}
        self.scheduler = BackgroundScheduler(jobstores=jobstores)
        self.scheduler.start()
        logger.info("✅ Scheduler initialized")
    
    def schedule_next_post(self):
        try:
            self.scheduler.remove_all_jobs()
            delay, next_time = self.publisher.calculate_next_post_time()
            self.scheduler.add_job(func=self.execute_post_job, trigger='interval', seconds=delay, id='post_job', replace_existing=True)
            logger.info(f"📅 Next: {next_time.strftime('%H:%M:%S')} ({delay // 60}min)")
            self.db.update_state('next_post_time', next_time.isoformat())
        except Exception as e:
            logger.error(f"❌ Schedule error: {e}")
    
    def execute_post_job(self):
        logger.info("=" * 60)
        logger.info("🎬 Post job")
        result = self.publisher.publish_post()
        logger.info(f"🎉 {result['title']}" if result['success'] else f"❌ {result.get('error')}")
        self.schedule_next_post()
        logger.info("=" * 60)
    
    def start(self):
        self.schedule_next_post()

app = Flask(__name__)
db_manager = publisher = scheduler = None
start_time = time.time()

MOBILE_POST_HTML = '''<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Post Now - She Cooks Bakes</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:600px;margin:0 auto;padding:20px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh}.container{background:white;border-radius:15px;padding:30px;box-shadow:0 10px 30px rgba(0,0,0,0.3)}h1{color:#2c3e50;text-align:center;margin-bottom:10px}.subtitle{text-align:center;color:#7f8c8d;margin-bottom:30px}button{width:100%;padding:15px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;border:none;border-radius:8px;font-size:18px;font-weight:bold;cursor:pointer;transition:all 0.3s}button:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(102,126,234,0.4)}button:disabled{background:#95a5a6;cursor:not-allowed}.result{margin-top:20px;padding:15px;border-radius:8px;display:none}.success{background:#d4edda;color:#155724}.error{background:#f8d7da;color:#721c24}.info{background:#e8f4f8;padding:15px;border-radius:8px;margin-bottom:20px;border-left:4px solid #667eea}</style></head><body><div class="container"><h1>🍰 She Cooks Bakes AI</h1><p class="subtitle">Powered by Google Gemini</p><div class="info"><strong>ℹ️ Info:</strong> Publish new post immediately.</div><button onclick="triggerPost()" id="postBtn">🚀 Publish Now</button><div id="result" class="result"></div></div><script>function triggerPost(){const btn=document.getElementById('postBtn');const result=document.getElementById('result');btn.disabled=true;btn.textContent='⏳ Publishing...';result.style.display='block';result.className='result';result.textContent='Creating post...';fetch('/post-now',{method:'POST',headers:{'Content-Type':'application/json'}}).then(r=>r.json()).then(d=>{if(d.result&&d.result.success){result.className='result success';result.innerHTML=`<strong>✅ Success!</strong><br>${d.result.title}<br>ID: ${d.result.post_id}`;}else{result.className='result error';result.innerHTML=`<strong>❌ Error:</strong> ${d.result?d.result.error:'Unknown'}`;}btn.disabled=false;btn.textContent='🚀 Publish Now';}).catch(e=>{result.className='result error';result.textContent='❌ '+e;btn.disabled=false;btn.textContent='🚀 Publish Now';});}</script></body></html>'''

@app.route('/')
def home():
    uptime = int(time.time() - start_time)
    return jsonify({"status": "active", "service": Config.APP_NAME, "version": Config.VERSION, "ai": "Gemini", "uptime": f"{uptime//3600}h{(uptime%3600)//60}m", "metrics": metrics.get_metrics(), "success_rate": f"{metrics.get_success_rate():.1f}%", "total_posts": db_manager.get_post_count(), "last_post": db_manager.get_last_post_time().isoformat() if db_manager.get_last_post_time() else "Never", "next_post": db_manager.get_state('next_post_time', 'Calculating...'), "blog": Config.BLOG_URL})

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat(), "database": "connected", "scheduler": "running", "ai": "Gemini"})

@app.route('/stats')
def stats():
    try:
        with db_manager.get_connection() as conn:
            recent = conn.execute('SELECT title, published_at, duration_seconds FROM posts ORDER BY published_at DESC LIMIT 5').fetchall()
            errors = conn.execute('SELECT COUNT(*) FROM errors WHERE DATE(occurred_at) = DATE("now")').fetchone()[0]
            avg = conn.execute('SELECT AVG(duration_seconds) FROM posts WHERE status = "success"').fetchone()[0] or 0
        return jsonify({"total_posts": db_manager.get_post_count(), "recent": [dict(r) for r in recent], "errors_today": errors, "avg_time": f"{avg:.1f}s", "performance": metrics.get_metrics()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/post-now', methods=['GET', 'POST'])
def manual_post():
    if request.method == 'GET':
        return render_template_string(MOBILE_POST_HTML)
    try:
        logger.info("🔧 Manual post")
        result = publisher.publish_post()
        scheduler.schedule_next_post()
        return jsonify({"manual_trigger": True, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/backup', methods=['POST'])
def create_backup():
    try:
        return jsonify({"created": db_manager.create_backup(), "time": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

class SelfPingService:
    def __init__(self):
        self.url = os.getenv('RENDER_EXTERNAL_URL', f'http://localhost:{Config.FLASK_PORT}')
        if Config.SELF_PING_ENABLED:
            threading.Thread(target=self._ping_loop, daemon=True).start()
            logger.info("💓 Self-ping started")
    def _ping_loop(self):
        while True:
            time.sleep(Config.SELF_PING_INTERVAL)
            try: 
                requests.get(f"{self.url}/health", timeout=10)
            except: 
                pass

def main():
    global db_manager, publisher, scheduler
    try:
        logger.info("=" * 60)
        logger.info(f"🚀 {Config.APP_NAME} v{Config.VERSION}")
        logger.info(f"🤖 AI: Google Gemini (Free)")
        logger.info(f"📸 Image: Professional Static")
        logger.info(f"📝 Content: SHORT & Promotional")
        logger.info("=" * 60)
        EnvironmentValidator.validate()
        db_manager = DatabaseManager()
        api_clients = APIClients()
        content_engine = ContentEngine(api_clients, db_manager)
        publisher = PublishingManager(content_engine, db_manager)
        scheduler = PostScheduler(publisher, db_manager)
        SelfPingService()
        scheduler.start()
        logger.info(f"🌐 Waitress on :{Config.FLASK_PORT}")
        logger.info("✅ OPERATIONAL!")
        logger.info("=" * 60)
        serve(app, host=Config.FLASK_HOST, port=Config.FLASK_PORT, threads=4, channel_timeout=60, cleanup_interval=30, _quiet=False)
    except KeyboardInterrupt:
        logger.info("⏹️ Shutdown...")
        if scheduler: scheduler.scheduler.shutdown()
        sys.exit(0)
    except Exception as e:
        logger.critical(f"💥 Critical: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
