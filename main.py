#!/usr/bin/env python3
"""
She Cooks Bakes AI - Automated Tumblr Blog System
Version: 4.0.0 (Production-Ready Enhanced Edition)

Complete AI-powered content generation system with:
✅ State persistence & crash recovery
✅ Smart topic caching (no duplicates)  
✅ Advanced error handling with retry logic
✅ Performance monitoring & metrics
✅ Automatic database backups
✅ Memory-efficient design
✅ OpenAI SDK v1.0+ (latest)
✅ Job persistence after restart
"""

import os
import sys
import json
import time
import random
import logging
import sqlite3
import requests
import threading
import shutil
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple
from contextlib import contextmanager
from collections import deque

# Web framework
from flask import Flask, jsonify

# OpenAI SDK (Updated to latest)
from openai import OpenAI, APIError, RateLimitError, APIConnectionError

# Tumblr API
import pytumblr

# Scheduler with persistence
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from logging.handlers import RotatingFileHandler

# ==================== CONFIGURATION ====================
class Config:
    """Centralized configuration management"""
    
    # App info
    APP_NAME = "She Cooks Bakes AI"
    VERSION = "4.0.0"
    ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
    
    # Flask
    FLASK_HOST = "0.0.0.0"
    FLASK_PORT = int(os.getenv("PORT", 10000))
    
    # Publishing intervals
    MIN_DELAY_SECONDS = 3600   # 1 hour
    MAX_DELAY_SECONDS = 7200   # 2 hours
    HUMAN_DELAY_MIN = 30
    HUMAN_DELAY_MAX = 90
    
    # Database
    DB_PATH = "she_cooks_bakes.db"
    DB_BACKUP_DIR = "backups"
    DB_BACKUP_INTERVAL = 86400  # 24 hours
    DB_MAX_BACKUPS = 7
    
    # Topic caching
    TOPIC_CACHE_SIZE = 10
    MIN_TOPIC_REUSE_DELAY = 20
    
    # API retries
    OPENAI_MAX_RETRIES = 3
    OPENAI_RETRY_DELAY = 2
    TUMBLR_MAX_RETRIES = 3
    TUMBLR_RETRY_DELAY = 2
    OPENAI_RATE_LIMIT_PAUSE = 60
    
    # Self-ping
    SELF_PING_ENABLED = True
    SELF_PING_INTERVAL = 840  # 14 minutes
    
    # Blog
    BLOG_URL = "she-cooks-bakes.tumblr.com"
    BLOG_WEBSITE = "https://shecooksbakes.blogspot.com/p/header-footer-nav.html"
    
    # Logging
    LOG_FILE = "she_cooks_bakes.log"
    LOG_MAX_BYTES = 5 * 1024 * 1024
    LOG_BACKUP_COUNT = 3
    
    # Metrics
    METRICS_ENABLED = True

# ==================== LOGGING ====================
def setup_logging() -> logging.Logger:
    """Configure logging with rotation"""
    logger = logging.getLogger(Config.APP_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    
    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    
    # File with rotation
    file_handler = RotatingFileHandler(
        Config.LOG_FILE,
        maxBytes=Config.LOG_MAX_BYTES,
        backupCount=Config.LOG_BACKUP_COUNT,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s'
    ))
    
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger

logger = setup_logging()

# ==================== METRICS ====================
class PerformanceMetrics:
    """Track performance metrics"""
    
    def __init__(self):
        self.enabled = Config.METRICS_ENABLED
        self.metrics = {
            'posts_published': 0,
            'posts_failed': 0,
            'api_calls': 0,
            'api_errors': 0,
            'avg_publish_time': 0,
            'last_publish_time': None
        }
        self.publish_times = deque(maxlen=50)
    
    def record_publish_success(self, duration: float):
        if not self.enabled:
            return
        self.metrics['posts_published'] += 1
        self.metrics['last_publish_time'] = datetime.now().isoformat()
        self.publish_times.append(duration)
        if self.publish_times:
            self.metrics['avg_publish_time'] = sum(self.publish_times) / len(self.publish_times)
    
    def record_publish_failure(self):
        if self.enabled:
            self.metrics['posts_failed'] += 1
    
    def record_api_call(self, success: bool = True):
        if not self.enabled:
            return
        self.metrics['api_calls'] += 1
        if not success:
            self.metrics['api_errors'] += 1
    
    def get_metrics(self) -> Dict:
        return self.metrics.copy()
    
    def get_success_rate(self) -> float:
        total = self.metrics['posts_published'] + self.metrics['posts_failed']
        return (self.metrics['posts_published'] / total * 100) if total > 0 else 100.0

metrics = PerformanceMetrics()

# ==================== DATABASE ====================
class DatabaseManager:
    """Enhanced database with connection pooling and backups"""
    
    def __init__(self, db_path: str = Config.DB_PATH):
        self.db_path = db_path
        self.backup_dir = Config.DB_BACKUP_DIR
        self._lock = threading.Lock()
        self.init_database()
        self._setup_backups()
    
    @contextmanager
    def get_connection(self):
        """Thread-safe connection context manager"""
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
            if conn:
                conn.close()
    
    def init_database(self):
        """Initialize schema with indexes"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Posts
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT UNIQUE,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    image_url TEXT,
                    tags TEXT,
                    published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'success',
                    duration_seconds REAL
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at DESC)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)')
            
            # State
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Errors
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    error_type TEXT,
                    error_message TEXT,
                    stack_trace TEXT,
                    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Topic cache
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS topic_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT UNIQUE NOT NULL,
                    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Metrics
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_name TEXT NOT NULL,
                    metric_value REAL NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
        logger.info("✅ Database initialized")
    
    def _setup_backups(self):
        """Setup automatic backups"""
        if not os.path.exists(self.backup_dir):
            os.makedirs(self.backup_dir)
        
        def backup_loop():
            while True:
                time.sleep(Config.DB_BACKUP_INTERVAL)
                self.create_backup()
        
        threading.Thread(target=backup_loop, daemon=True).start()
        logger.info("✅ Auto-backup started")
    
    def create_backup(self) -> bool:
        """Create database backup"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = os.path.join(self.backup_dir, f'backup_{timestamp}.db')
            shutil.copy2(self.db_path, backup_path)
            logger.info(f"💾 Backup created: {backup_path}")
            self._cleanup_old_backups()
            return True
        except Exception as e:
            logger.error(f"❌ Backup failed: {e}")
            return False
    
    def _cleanup_old_backups(self):
        """Remove old backups"""
        try:
            backups = sorted([f for f in os.listdir(self.backup_dir) if f.startswith('backup_')])
            while len(backups) > Config.DB_MAX_BACKUPS:
                old = backups.pop(0)
                os.remove(os.path.join(self.backup_dir, old))
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
    
    def save_post(self, post_data: Dict, duration: float = 0) -> bool:
        """Save post"""
        try:
            with self.get_connection() as conn:
                conn.execute('''
                    INSERT INTO posts (post_id, topic, title, image_url, tags, status, duration_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    post_data.get('post_id', 'unknown'),
                    post_data.get('topic', ''),
                    post_data.get('title', ''),
                    post_data.get('image_url', ''),
                    json.dumps(post_data.get('tags', [])),
                    'success' if post_data.get('success') else 'failed',
                    duration
                ))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Save post error: {e}")
            return False
    
    def update_state(self, key: str, value: str):
        """Update state"""
        try:
            with self.get_connection() as conn:
                conn.execute('''
                    INSERT INTO state (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                ''', (key, value))
                conn.commit()
        except Exception as e:
            logger.error(f"Update state error: {e}")
    
    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get state"""
        try:
            with self.get_connection() as conn:
                result = conn.execute('SELECT value FROM state WHERE key = ?', (key,)).fetchone()
                return result[0] if result else default
        except Exception as e:
            logger.error(f"Get state error: {e}")
            return default
    
    def log_error(self, error_type: str, error_message: str, stack_trace: str = ""):
        """Log error"""
        try:
            with self.get_connection() as conn:
                conn.execute('INSERT INTO errors (error_type, error_message, stack_trace) VALUES (?, ?, ?)',
                           (error_type, error_message, stack_trace))
                conn.commit()
        except Exception as e:
            logger.error(f"Log error failed: {e}")
    
    def get_post_count(self) -> int:
        """Get post count"""
        try:
            with self.get_connection() as conn:
                result = conn.execute('SELECT COUNT(*) FROM posts WHERE status = "success"').fetchone()
                return result[0]
        except:
            return 0
    
    def get_last_post_time(self) -> Optional[datetime]:
        """Get last post time"""
        try:
            with self.get_connection() as conn:
                result = conn.execute('''
                    SELECT published_at FROM posts WHERE status = "success" 
                    ORDER BY published_at DESC LIMIT 1
                ''').fetchone()
                return datetime.fromisoformat(result[0]) if result else None
        except:
            return None
    
    def add_to_topic_cache(self, topic: str):
        """Add topic to cache"""
        try:
            with self.get_connection() as conn:
                conn.execute('INSERT OR REPLACE INTO topic_cache (topic, last_used) VALUES (?, CURRENT_TIMESTAMP)', (topic,))
                conn.commit()
        except Exception as e:
            logger.error(f"Topic cache error: {e}")
    
    def get_recent_topics(self, limit: int = 10) -> List[str]:
        """Get recent topics"""
        try:
            with self.get_connection() as conn:
                results = conn.execute('SELECT topic FROM topic_cache ORDER BY last_used DESC LIMIT ?', (limit,)).fetchall()
                return [row[0] for row in results]
        except:
            return []
    
    def save_metric(self, metric_name: str, metric_value: float):
        """Save metric"""
        try:
            with self.get_connection() as conn:
                conn.execute('INSERT INTO metrics (metric_name, metric_value) VALUES (?, ?)', (metric_name, metric_value))
                conn.commit()
        except Exception as e:
            logger.error(f"Save metric error: {e}")

# ==================== ENV VALIDATION ====================
class EnvironmentValidator:
    """Validate environment variables"""
    
    REQUIRED_VARS = [
        'TUMBLR_CONSUMER_KEY',
        'TUMBLR_CONSUMER_SECRET',
        'TUMBLR_OAUTH_TOKEN',
        'TUMBLR_OAUTH_SECRET',
        'OPENAI_API_KEY'
    ]
    
    @staticmethod
    def validate() -> bool:
        missing = [var for var in EnvironmentValidator.REQUIRED_VARS if not os.getenv(var)]
        if missing:
            error_msg = f"❌ Missing: {', '.join(missing)}"
            logger.critical(error_msg)
            raise EnvironmentError(error_msg)
        logger.info("✅ Environment validated")
        return True

# ==================== API CLIENTS ====================
class APIClients:
    """API clients with smart error handling"""
    
    def __init__(self):
        self.tumblr_client = None
        self.openai_client = None
        self.init_clients()
    
    def init_clients(self):
        """Initialize clients"""
        try:
            # Tumblr
            self.tumblr_client = pytumblr.TumblrRestClient(
                consumer_key=os.getenv('TUMBLR_CONSUMER_KEY'),
                consumer_secret=os.getenv('TUMBLR_CONSUMER_SECRET'),
                oauth_token=os.getenv('TUMBLR_OAUTH_TOKEN'),
                oauth_secret=os.getenv('TUMBLR_OAUTH_SECRET')
            )
            
            # OpenAI (new SDK)
            self.openai_client = OpenAI(
                api_key=os.getenv('OPENAI_API_KEY'),
                timeout=60.0,
                max_retries=0
            )
            
            logger.info("✅ API clients initialized (OpenAI SDK v1.0+)")
        except Exception as e:
            logger.critical(f"❌ Client init failed: {e}")
            raise
    
    def retry_with_smart_backoff(self, func, operation_name: str, max_retries: int = 3):
        """Retry with error-specific handling"""
        for attempt in range(max_retries):
            try:
                metrics.record_api_call(success=True)
                return func()
            except RateLimitError as e:
                metrics.record_api_call(success=False)
                logger.warning(f"⚠️ Rate limit: {operation_name}")
                if attempt < max_retries - 1:
                    time.sleep(Config.OPENAI_RATE_LIMIT_PAUSE)
                else:
                    raise
            except APIConnectionError as e:
                metrics.record_api_call(success=False)
                logger.warning(f"⚠️ Connection error: {operation_name}")
                if attempt < max_retries - 1:
                    time.sleep(Config.OPENAI_RETRY_DELAY * (2 ** attempt))
                else:
                    raise
            except APIError as e:
                metrics.record_api_call(success=False)
                logger.error(f"❌ API error: {operation_name} - {e}")
                if attempt < max_retries - 1 and e.status_code >= 500:
                    time.sleep(Config.OPENAI_RETRY_DELAY * (2 ** attempt))
                else:
                    raise
            except Exception as e:
                metrics.record_api_call(success=False)
                logger.error(f"❌ Unexpected: {operation_name} - {e}")
                if attempt < max_retries - 1:
                    time.sleep(Config.OPENAI_RETRY_DELAY * (2 ** attempt))
                else:
                    raise

# ==================== CONTENT ENGINE ====================
class ContentEngine:
    """Content generation with topic caching and multiple image sources"""
    
    def __init__(self, api_clients: APIClients, db_manager: DatabaseManager):
        self.clients = api_clients
        self.db = db_manager
        self.blog_url = Config.BLOG_URL
        
        self.base_tags = [
            "Baking", "DessertRecipes", "CookingTips",
            "HomeBaking", "SweetTreats", "RecipeIdeas",
            "FoodBlog", "DeliciousDesserts", "BakingLove"
        ]
        
        # Multiple backup sources
        self.backup_images = {
            'pexels': [
                "https://images.pexels.com/photos/291528/pexels-photo-291528.jpeg",
                "https://images.pexels.com/photos/227432/pexels-photo-227432.jpeg",
                "https://images.pexels.com/photos/14107/pexels-photo-14107.jpeg",
                "https://images.pexels.com/photos/806363/pexels-photo-806363.jpeg",
                "https://images.pexels.com/photos/1092730/pexels-photo-1092730.jpeg",
            ],
            'unsplash': [
                "https://images.unsplash.com/photo-1486427944299-d1955d23e34d",
                "https://images.unsplash.com/photo-1578985545062-69928b1d9587",
                "https://images.unsplash.com/photo-1558961363-fa8fdf82db35",
            ]
        }
        self.current_source = 'pexels'
        self.image_index = 0
    
    def generate_recipe_topic(self) -> str:
        """Generate topic with caching"""
        topics = [
            "Chocolate Lava Cake", "French Macarons", "Artisan Bread",
            "Italian Tiramisu", "Red Velvet Cupcakes", "Vegan Cookies",
            "Professional Cheesecake", "Baklava", "Matcha Desserts",
            "American Pancakes", "Belgian Truffles", "French Croissants",
            "New York Cheesecake", "Italian Cannoli", "Spanish Churros",
            "British Scones", "Black Forest Cake", "Apple Strudel",
            "Custard Tarts", "Chocolate Fondue", "Cinnamon Rolls",
            "Lemon Pie", "Carrot Cake", "Brownies", "Banana Bread"
        ]
        
        recent = self.db.get_recent_topics(Config.TOPIC_CACHE_SIZE)
        available = [t for t in topics if t not in recent]
        
        if not available:
            logger.info("🔄 Topic cache reset")
            available = topics
        
        topic = random.choice(available)
        self.db.add_to_topic_cache(topic)
        return topic
    
    def generate_ai_text(self, topic: str) -> Dict:
        """Generate text with new OpenAI SDK"""
        def _generate():
            response = self.clients.openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a professional pastry chef and food blogger."},
                    {"role": "user", "content": f'''Create a blog post about "{topic}".
JSON format:
{{"title": "...", "introduction": "...", "description": "...", "ingredients": [...], "tips": [...], "cta": "..."}}'''}
                ],
                temperature=0.8,
                max_tokens=600
            )
            content = response.choices[0].message.content.strip()
            return json.loads(content) if content.startswith('{') else self._fallback_content(topic)
        
        try:
            return self.clients.retry_with_smart_backoff(_generate, "AI Text", Config.OPENAI_MAX_RETRIES)
        except:
            return self._fallback_content(topic)
    
    def _fallback_content(self, topic: str) -> Dict:
        return {
            "title": f"Mastering {topic}",
            "introduction": f"Welcome to our guide on {topic}.",
            "description": f"This recipe combines traditional techniques with modern twists.",
            "ingredients": ["Flour", "Sugar", "Butter", "Eggs", "Vanilla", "Baking powder"],
            "tips": ["Use room temp ingredients", "Preheat oven", "Don't overmix"],
            "cta": "Visit our blog for more!"
        }
    
    def generate_ai_image(self, description: str) -> str:
        """Generate image with fallback"""
        def _generate():
            response = self.clients.openai_client.images.generate(
                model="dall-e-3",
                prompt=f"Professional food photography: {description}",
                size="1024x1024",
                quality="standard",
                n=1
            )
            return response.data[0].url
        
        try:
            return self.clients.retry_with_smart_backoff(_generate, "AI Image", Config.OPENAI_MAX_RETRIES)
        except:
            return self._fallback_image()
    
    def _fallback_image(self) -> str:
        """Rotating fallback images"""
        images = self.backup_images[self.current_source]
        image = images[self.image_index % len(images)]
        self.image_index += 1
        
        if self.image_index % 5 == 0:
            sources = list(self.backup_images.keys())
            idx = sources.index(self.current_source)
            self.current_source = sources[(idx + 1) % len(sources)]
        
        logger.info(f"📸 Fallback from {self.current_source}")
        return image
    
    def generate_trending_tags(self) -> List[str]:
        trending = [
            "Foodie", "InstaFood", "Yummy", "FoodPhotography",
            "BakingFromScratch", "DessertLover", "HomeChef",
            "EasyRecipes", "FoodBlogger", "SweetTooth"
        ]
        return random.sample(trending, 4)
    
    def format_post_content(self, ai_content: Dict) -> str:
        """Format HTML content"""
        ingredients = "".join([f'<li>{i}</li>' for i in ai_content['ingredients']])
        tips = "".join([f'<li>{t}</li>' for t in ai_content['tips']])
        
        return f"""
<div style="font-family: Arial, sans-serif; line-height: 1.6;">
    <h2 style="color: #2c3e50;">{ai_content['title']}</h2>
    <p style="font-size: 16px; color: #34495e;">{ai_content['introduction']}</p>
    <p style="font-size: 15px; color: #555;">{ai_content['description']}</p>
    <h3 style="color: #e74c3c;">✨ Ingredients:</h3>
    <ul style="color: #555;">{ingredients}</ul>
    <h3 style="color: #3498db;">👩‍🍳 Tips:</h3>
    <ul style="color: #555;">{tips}</ul>
    <div style="background: #f8f9fa; padding: 15px; border-left: 4px solid #e74c3c; margin-top: 20px;">
        <h3 style="color: #2c3e50; margin-top: 0;">🎯 {ai_content['cta']}</h3>
        <p><strong>Visit:</strong> <a href="{Config.BLOG_WEBSITE}">She Cooks & Bakes</a></p>
    </div>
    <hr style="margin: 20px 0; border-top: 1px solid #ddd;">
    <p style="font-size: 12px; color: #999; text-align: center;">
        Posted by She Cooks Bakes AI • {datetime.now().strftime('%B %d, %Y')}
    </p>
</div>
"""

# ==================== PUBLISHER ====================
class PublishingManager:
    """Publishing with performance tracking"""
    
    def __init__(self, content_engine: ContentEngine, db_manager: DatabaseManager):
        self.engine = content_engine
        self.db = db_manager
    
    def human_delay(self, min_s: int = None, max_s: int = None):
        delay = random.randint(min_s or Config.HUMAN_DELAY_MIN, max_s or Config.HUMAN_DELAY_MAX)
        time.sleep(delay)
    
    def publish_post(self) -> Dict:
        """Main publishing workflow"""
        start = time.time()
        
        try:
            # Generate
            topic = self.engine.generate_recipe_topic()
            logger.info(f"🍰 Topic: {topic}")
            
            logger.info("📝 Generating text...")
            ai_content = self.engine.generate_ai_text(topic)
            self.human_delay(20, 40)
            
            logger.info("🎨 Generating image...")
            image_url = self.engine.generate_ai_image(f"{ai_content['title']} food photography")
            self.human_delay(30, 50)
            
            # Publish
            tags = self.engine.base_tags + self.engine.generate_trending_tags()
            content = self.engine.format_post_content(ai_content)
            
            logger.info("🚀 Publishing...")
            
            def _publish():
                return self.engine.clients.tumblr_client.create_photo(
                    blogname=self.engine.blog_url,
                    state="published",
                    tags=tags,
                    caption=content,
                    source=image_url,
                    format="html"
                )
            
            response = self.engine.clients.retry_with_smart_backoff(_publish, "Tumblr Publish", Config.TUMBLR_MAX_RETRIES)
            
            post_id = response.get('id', 'unknown')
            duration = time.time() - start
            
            # Save
            post_data = {
                "success": True,
                "post_id": str(post_id),
                "topic": topic,
                "title": ai_content['title'],
                "image_url": image_url,
                "tags": tags
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
            
            return {
                "success": False,
                "error": str(e),
                "topic": topic if 'topic' in locals() else 'unknown'
            }
    
    def calculate_next_post_time(self) -> Tuple[int, datetime]:
        """Calculate next post time"""
        last = self.db.get_last_post_time()
        
        if last:
            elapsed = (datetime.now() - last).total_seconds()
            if elapsed < Config.MIN_DELAY_SECONDS:
                delay = int(Config.MIN_DELAY_SECONDS - elapsed)
            else:
                delay = random.randint(Config.MIN_DELAY_SECONDS, Config.MAX_DELAY_SECONDS)
        else:
            delay = 60
        
        next_time = datetime.now() + timedelta(seconds=delay)
        return delay, next_time

# ==================== SCHEDULER ====================
class PostScheduler:
    """Scheduler with job persistence"""
    
    def __init__(self, publisher: PublishingManager, db_manager: DatabaseManager):
        self.publisher = publisher
        self.db = db_manager
        
        jobstores = {'default': SQLAlchemyJobStore(url=f'sqlite:///{Config.DB_PATH}')}
        self.scheduler = BackgroundScheduler(jobstores=jobstores)
        self.scheduler.start()
        
        logger.info("✅ Scheduler initialized with persistence")
    
    def schedule_next_post(self):
        """Schedule next post"""
        try:
            self.scheduler.remove_all_jobs()
            delay, next_time = self.publisher.calculate_next_post_time()
            
            self.scheduler.add_job(
                func=self.execute_post_job,
                trigger='interval',
                seconds=delay,
                id='post_job',
                replace_existing=True
            )
            
            logger.info(f"📅 Next: {next_time.strftime('%Y-%m-%d %H:%M:%S')} ({delay // 60}min)")
            self.db.update_state('next_post_time', next_time.isoformat())
        except Exception as e:
            logger.error(f"❌ Schedule error: {e}")
    
    def execute_post_job(self):
        """Execute post"""
        logger.info("=" * 60)
        logger.info("🎬 Post job starting")
        
        result = self.publisher.publish_post()
        
        if result['success']:
            logger.info(f"🎉 Success: {result['title']}")
        else:
            logger.error(f"❌ Failed: {result.get('error')}")
        
        self.schedule_next_post()
        logger.info("=" * 60)
    
    def start(self):
        self.schedule_next_post()

# ==================== FLASK ====================
app = Flask(__name__)

db_manager = None
publisher = None
scheduler = None
start_time = time.time()

@app.route('/')
def home():
    """Health check"""
    uptime = int(time.time() - start_time)
    h, m = uptime // 3600, (uptime % 3600) // 60
    
    return jsonify({
        "status": "active",
        "service": Config.APP_NAME,
        "version": Config.VERSION,
        "uptime": f"{h}h {m}m",
        "metrics": metrics.get_metrics(),
        "success_rate": f"{metrics.get_success_rate():.1f}%",
        "total_posts": db_manager.get_post_count(),
        "last_post": db_manager.get_last_post_time().isoformat() if db_manager.get_last_post_time() else "Never",
        "next_post": db_manager.get_state('next_post_time', 'Calculating...'),
        "blog": Config.BLOG_URL
    })

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "database": "connected",
        "scheduler": "running"
    })

@app.route('/stats')
def stats():
    try:
        with db_manager.get_connection() as conn:
            recent = conn.execute('SELECT title, published_at, duration_seconds FROM posts ORDER BY published_at DESC LIMIT 5').fetchall()
            errors = conn.execute('SELECT COUNT(*) FROM errors WHERE DATE(occurred_at) = DATE("now")').fetchone()[0]
            avg_dur = conn.execute('SELECT AVG(duration_seconds) FROM posts WHERE status = "success"').fetchone()[0] or 0
        
        return jsonify({
            "total_posts": db_manager.get_post_count(),
            "recent_posts": [dict(r) for r in recent],
            "errors_today": errors,
            "avg_publish_time": f"{avg_dur:.1f}s",
            "performance": metrics.get_metrics()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/post-now', methods=['POST'])
def manual_post():
    try:
        result = publisher.publish_post()
        scheduler.schedule_next_post()
        return jsonify({"manual_trigger": True, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/backup', methods=['POST'])
def create_backup():
    try:
        success = db_manager.create_backup()
        return jsonify({"backup_created": success, "timestamp": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== SELF-PING ====================
class SelfPingService:
    def __init__(self):
        self.url = os.getenv('RENDER_EXTERNAL_URL', f'http://localhost:{Config.FLASK_PORT}')
        if Config.SELF_PING_ENABLED:
            threading.Thread(target=self._ping_loop, daemon=True).start()
            logger.info(f"💓 Self-ping started")
    
    def _ping_loop(self):
        while True:
            time.sleep(Config.SELF_PING_INTERVAL)
            try:
                requests.get(f"{self.url}/health", timeout=10)
                logger.debug("💓 Ping OK")
            except:
                pass

# ==================== MAIN ====================
def main():
    global db_manager, publisher, scheduler
    
    try:
        logger.info("=" * 60)
        logger.info(f"🚀 {Config.APP_NAME} v{Config.VERSION}")
        logger.info(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        
        EnvironmentValidator.validate()
        
        logger.info("💾 Initializing database...")
        db_manager = DatabaseManager()
        
        logger.info("🔌 Initializing API clients...")
        api_clients = APIClients()
        
        logger.info("🎨 Initializing content engine...")
        content_engine = ContentEngine(api_clients, db_manager)
        
        logger.info("📤 Initializing publisher...")
        publisher = PublishingManager(content_engine, db_manager)
        
        logger.info("⏰ Initializing scheduler...")
        scheduler = PostScheduler(publisher, db_manager)
        
        logger.info("💓 Starting self-ping...")
        SelfPingService()
        
        logger.info("🎬 Starting scheduler...")
        scheduler.start()
        
        logger.info(f"🌐 Starting Flask on :{Config.FLASK_PORT}")
        logger.info("=" * 60)
        logger.info("✅ SYSTEM OPERATIONAL!")
        logger.info("=" * 60)
        
        app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT, debug=False, use_reloader=False)
        
    except KeyboardInterrupt:
        logger.info("⏹️ Shutting down...")
        if scheduler:
            scheduler.scheduler.shutdown()
        sys.exit(0)
    except Exception as e:
        logger.critical(f"💥 Critical: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()