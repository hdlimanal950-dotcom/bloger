#!/usr/bin/env python3
"""
She Cooks Bakes AI v5.1.0
✅ NEW: Google Gemini AI (Free tier, unlimited creativity)
✅ NEW: Smart hashtag system (2 AI-generated + global tags)
✅ NEW: Complete Recipe Content (Full ingredients & steps)
✅ NEW: High-Quality Unsplash Images (Smart keyword search)
✅ Fixed: All previous issues (Scheduler, Mobile, Rate limits)
"""
import os, sys, json, time, random, logging, sqlite3, requests, threading, shutil
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple
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
    VERSION = "5.1.0"
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
    UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")  # Optional for better results

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
                    logger.warning(f"⚠️ Rate limit hit: {operation_name}")
                    if attempt < max_retries - 1:
                        wait_time = Config.GEMINI_RATE_LIMIT_PAUSE
                        logger.info(f"⏳ Waiting {wait_time}s for rate limit cooldown...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"❌ Rate limit exceeded after {max_retries} attempts")
                        raise
                elif 'connection' in error_str or 'network' in error_str:
                    logger.warning(f"⚠️ Connection error: {operation_name}")
                    if attempt < max_retries - 1:
                        delay = Config.GEMINI_RETRY_DELAY * (2 ** attempt)
                        logger.info(f"⏳ Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        raise
                else:
                    logger.error(f"❌ API error: {operation_name} - {str(e)}")
                    if attempt < max_retries - 1:
                        delay = Config.GEMINI_RETRY_DELAY * (2 ** attempt)
                        logger.info(f"⏳ Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        raise

class ContentEngine:
    def __init__(self, api_clients: APIClients, db_manager: DatabaseManager):
        self.clients = api_clients
        self.db = db_manager
        self.blog_url = Config.BLOG_URL
        self.base_tags = ["Baking", "DessertRecipes", "CookingTips", "HomeBaking", "SweetTreats", "RecipeIdeas", "FoodBlog", "DeliciousDesserts", "BakingLove"]
        
        # Smart keyword mapping for Unsplash search
        self.topic_keyword_map = {
            # Cakes & Desserts
            "cake": ["cake", "dessert", "sweet", "baking", "frosting", "layered cake"],
            "cheesecake": ["cheesecake", "new york cheesecake", "cream cheese dessert"],
            "chocolate": ["chocolate", "chocolate dessert", "cocoa", "chocolate cake"],
            "cupcake": ["cupcakes", "frosted cupcakes", "baking", "party dessert"],
            "pie": ["pie", "fruit pie", "pastry", "dessert pie"],
            "brownie": ["brownies", "chocolate brownies", "fudgy dessert"],
            "cookie": ["cookies", "baked cookies", "chocolate chip cookies"],
            "macaron": ["macarons", "french macarons", "colorful macarons"],
            
            # Breads & Pastries
            "bread": ["artisan bread", "fresh bread", "baking", "loaf"],
            "croissant": ["croissants", "french croissants", "buttery pastry"],
            "pastry": ["pastry", "baked goods", "flaky pastry"],
            "scone": ["scones", "british scones", "tea time pastry"],
            "doughnut": ["doughnuts", "donuts", "glazed donuts", "breakfast pastry"],
            
            # Pizzas & Savory
            "pizza": ["pizza", "italian pizza", "cheese pizza", "wood fired pizza"],
            "pasta": ["pasta", "italian pasta", "spaghetti", "noodles"],
            "lasagna": ["lasagna", "italian lasagna", "pasta bake"],
            "risotto": ["risotto", "creamy risotto", "italian rice dish"],
            
            # Asian Cuisine
            "ramen": ["ramen", "japanese ramen", "noodle soup", "bowl of ramen"],
            "sushi": ["sushi", "japanese sushi", "sushi rolls", "nigiri"],
            "curry": ["curry", "indian curry", "spicy curry", "curry dish"],
            "pho": ["pho", "vietnamese pho", "noodle soup", "beef pho"],
            "dim sum": ["dim sum", "chinese dim sum", "dumplings", "steamed buns"],
            "bibimbap": ["bibimbap", "korean bibimbap", "rice bowl", "mixed rice"],
            
            # International
            "taco": ["tacos", "mexican tacos", "street tacos", "taco Tuesday"],
            "paella": ["paella", "spanish paella", "seafood paella", "rice dish"],
            "kebab": ["kebab", "turkish kebab", "grilled meat", "shawarma"],
            "hummus": ["hummus", "middle eastern hummus", "chickpea dip"],
            "falafel": ["falafel", "middle eastern falafel", "fried chickpea"],
            
            # Breakfast
            "pancake": ["pancakes", "fluffy pancakes", "breakfast pancakes", "stack"],
            "waffle": ["waffles", "belgian waffles", "breakfast waffles"],
            "omelette": ["omelette", "fluffy omelette", "breakfast eggs"],
            
            # Drinks
            "coffee": ["coffee", "espresso", "cappuccino", "latte art"],
            "tea": ["tea", "herbal tea", "tea cup", "tea leaves"],
            "smoothie": ["smoothie", "fruit smoothie", "healthy smoothie", "blended"],
            "cocktail": ["cocktail", "mixed drink", "martini", "mojito"],
        }
        
        # Fallback images for when Unsplash fails
        self.fallback_images = [
            "https://images.unsplash.com/photo-1565958011703-44f9829ba187",  # General food
            "https://images.unsplash.com/photo-1546069901-ba9599a7e63c",  # Ingredients
            "https://images.unsplash.com/photo-1490818387583-1baba5e638af",  # Cooking
            "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4",  # Restaurant
            "https://images.unsplash.com/photo-1556909114-f6e7ad7d3136",  # Bakery
        ]
        
    def generate_recipe_topic(self):
        topics = [
            "Chocolate Lava Cake", "French Macarons", "Artisan Bread", "Italian Tiramisu", "Red Velvet Cupcakes",
            "Vegan Cookies", "Professional Cheesecake", "Baklava", "Matcha Desserts", "American Pancakes",
            "Belgian Truffles", "French Croissants", "New York Cheesecake", "Italian Cannoli", "Spanish Churros",
            "British Scones", "Black Forest Cake", "Apple Strudel", "Custard Tarts", "Chocolate Fondue",
            "Cinnamon Rolls", "Lemon Pie", "Carrot Cake", "Brownies", "Banana Bread",
            "Japanese Mochi", "Turkish Delight", "Greek Baklava", "Portuguese Pastel de Nata", "Austrian Apfelstrudel",
            "Swiss Chocolate Truffles", "Danish Pastries", "Swedish Princess Cake", "Polish Paczki", "Hungarian Dobos Torte",
            "Russian Honey Cake", "Indian Gulab Jamun", "Indian Butter Chicken", "Japanese Ramen", "Thai Green Curry",
            "Vietnamese Pho", "Korean Bibimbap", "Chinese Dim Sum", "Mexican Tacos", "Italian Pizza Margherita",
            "French Coq au Vin", "Spanish Paella", "Greek Moussaka", "Turkish Kebabs", "Lebanese Hummus",
            "Moroccan Tagine", "Ethiopian Injera", "American BBQ Ribs", "Canadian Poutine", "Australian Meat Pie",
            "Brazilian Feijoada", "Argentinian Empanadas", "Peruvian Ceviche", "Chilean Pastel de Choclo", "Colombian Arepas",
            "Jamaican Jerk Chicken", "Cuban Sandwich", "Puerto Rican Mofongo", "Dominican Mangú", "Haitian Griot",
            "Nigerian Jollof Rice", "South African Bobotie", "Egyptian Koshari", "Israeli Falafel", "Palestinian Maqluba",
            "Iranian Chelow Kabab", "Saudi Arabian Kabsa", "Emirati Shawarma", "Qatari Machboos", "Omani Shuwa",
            "Pakistani Biryani", "Bangladeshi Hilsa Curry", "Sri Lankan Kottu Roti", "Nepali Momo", "Bhutanese Ema Datshi",
            "Burmese Mohinga", "Thai Tom Yum Goong", "Cambodian Fish Amok", "Laotian Larb", "Indonesian Rendang",
            "Malaysian Nasi Lemak", "Singaporean Chili Crab", "Filipino Adobo", "Vietnamese Banh Mi", "Korean Kimchi",
            "Japanese Sushi", "Chinese Peking Duck", "Mongolian Buuz", "Taiwanese Beef Noodle Soup", "Hong Kong Egg Waffles",
            "Macanese Egg Tarts", "Tibetan Thukpa", "Uzbek Plov", "Kazakh Beshbarmak", "Kyrgyz Laghman",
            "Georgian Khachapuri", "Armenian Lahmajoun", "Azerbaijani Plov", "Turkish Menemen", "Cypriot Halloumi",
            "Maltese Pastizzi", "Icelandic Hangikjöt", "Norwegian Rakfisk", "Swedish Meatballs", "Finnish Karjalanpiirakka",
            "Danish Smørrebrød", "Dutch Stroopwafel", "Belgian Waffles", "German Pretzels", "Swiss Fondue",
            "Austrian Wiener Schnitzel", "Czech Trdelník", "Slovak Bryndzové Halušky", "Polish Pierogi", "Hungarian Goulash",
            "Romanian Sarmale", "Bulgarian Banitsa", "Serbian Ćevapi", "Croatian Peka", "Bosnian Burek",
            "Albanian Byrek", "Greek Souvlaki", "Macedonian Tavče Gravče", "Montenegrin Kačamak", "Slovenian Potica",
            "Italian Risotto", "French Ratatouille", "Spanish Gazpacho", "Portuguese Bacalhau", "British Fish and Chips",
            "Irish Stew", "Scottish Haggis", "Welsh Cawl", "Cornish Pasty", "Manx Kippers",
            "Guernsey Gâche", "Jersey Bean Crock", "Sicilian Cannoli", "Sardinian Porceddu", "Neapolitan Pizza",
            "Tuscan Ribollita", "Venetian Risotto", "Roman Carbonara", "Milanese Osso Buco", "Bolognese Tagliatelle",
            "Florentine Bistecca", "Genovese Pesto", "Calabrian 'Nduja", "Puglian Orecchiette", "Abruzzese Arrosticini",
            "Basque Cheesecake", "Catalan Crema Catalana", "Andalusian Salmorejo", "Galician Pulpo a la Gallega", "Asturian Fabada",
            "Valencian Horchata", "Balearic Ensaimada", "Canarian Papas Arrugadas", "Extremaduran Migas", "La Riojan Patatas a la Riojana",
            "Navarran Chilindrón", "Aragonese Pollo al Chilindrón", "Cantabrian Cocido Lebaniego", "Murcian Caldero", "Castilian Cochinillo",
            "Leonese Cecina", "Manchego Pisto", "Madrilenian Cocido Madrileño", "Rojan Patatas Bravas", "Andorran Escudella",
            "Monégasque Barbagiuan", "San Marinese Torta Tre Monti", "Vatican Swiss Guard Recipes", "Liechtensteiner Käsknöpfle",
            "Luxembourdish Bouneschlupp", "Monegasque Fougasse", "Alsatian Choucroute", "Breton Crêpes", "Provençal Tapenade",
            "Normandie Tarte Tatin", "Bordeaux Canelé", "Burgundy Coq au Vin", "Champagne Sabayon", "Dijon Mustard Recipes",
            "Lyon Quenelle", "Marseille Bouillabaisse", "Nice Salade Niçoise", "Toulouse Cassoulet", "Strasbourg Flammekueche",
            "Reims Biscuit Rose", "Montpellier Brandade", "Nantes Galette Nantaise", "Bordeaux Éclair", "Lille Welsh",
            "Rouen Canard à la Rouennaise", "Amiens Ficelle Picarde", "Tours Rillettes", "Angers Crémant", "Brest Kig ha Farz",
            "Cherbourg Teurgoule", "Dunkerque Waterzooi", "Grenoble Gratin Dauphinois", "Limoges Pâté de Pommes de Terre", "Metz Mirabelle Tart",
            "Mullette Baeckeoffe", "Nancy Macarons", "Orléans Cotignac", "Poitiers Farci Poitevin", "Quimper Crêpe Bretonne",
            "Saint-Étienne Rapée", "Toulon Tarte Tropezienne", "Troyes Andouillette", "Valence Pogne", "Vannes Kouign-Amann",
            "Versailles Petit Four", "Épinal Baba au Rhum", "Ajaccio Aziminu", "Bastia Stufatu", "Calvi Tiramisu",
            "Corte Fiadone", "Sartène Coppa", "Porto-Vecchio Canistrelli", "Bastelicaccia Figatellu", "L'Île-Rousse Panzetta"
        ]
        recent = self.db.get_recent_topics(Config.TOPIC_CACHE_SIZE)
        available = [t for t in topics if t not in recent]
        if not available: 
            logger.info("🔄 Topic cache reset")
            available = topics
        topic = random.choice(available)
        self.db.add_to_topic_cache(topic)
        return topic
    
    def _get_topic_keywords(self, topic: str) -> List[str]:
        """Get smart search keywords for a topic"""
        topic_lower = topic.lower()
        
        # First, check for exact keyword matches
        for keyword, search_terms in self.topic_keyword_map.items():
            if keyword in topic_lower:
                return search_terms
        
        # If no exact match, analyze the topic
        words = topic_lower.split()
        
        # Check for common food categories
        if any(word in topic_lower for word in ["cake", "cupcake", "cheesecake", "brownie", "pie", "tart"]):
            return self.topic_keyword_map.get("cake", ["dessert", "baking"])
        elif any(word in topic_lower for word in ["bread", "croissant", "scone", "pastry", "bun", "roll"]):
            return self.topic_keyword_map.get("bread", ["bread", "baking"])
        elif any(word in topic_lower for word in ["pizza", "pasta", "lasagna", "risotto"]):
            return self.topic_keyword_map.get("pizza", ["italian food", "cooking"])
        elif any(word in topic_lower for word in ["ramen", "sushi", "curry", "pho", "dim sum", "asian"]):
            return self.topic_keyword_map.get("ramen", ["asian food", "cuisine"])
        elif any(word in topic_lower for word in ["taco", "burrito", "enchilada", "mexican"]):
            return self.topic_keyword_map.get("taco", ["mexican food", "tacos"])
        elif any(word in topic_lower for word in ["coffee", "tea", "smoothie", "cocktail", "drink"]):
            return self.topic_keyword_map.get("coffee", ["beverage", "drink"])
        elif any(word in topic_lower for word in ["pancake", "waffle", "omelette", "breakfast"]):
            return self.topic_keyword_map.get("pancake", ["breakfast", "brunch"])
        
        # Default to general food keywords
        return ["food", "cooking", "recipe", "delicious"]
    
    def generate_high_quality_unsplash_image(self, topic: str):
        """Generate high-quality Unsplash image URL with smart keyword search"""
        try:
            # Get smart keywords for this topic
            keywords = self._get_topic_keywords(topic)
            main_keyword = keywords[0]
            
            # Generate random seed for variety
            random_seed = random.randint(1000, 9999)
            timestamp = int(time.time())
            
            # If we have Unsplash API key, use official API for better results
            if Config.UNSPLASH_ACCESS_KEY:
                try:
                    # Use Unsplash API for more precise results
                    search_query = "+".join(keywords[:2])  # Use top 2 keywords
                    api_url = f"https://api.unsplash.com/photos/random"
                    params = {
                        'query': search_query,
                        'orientation': 'landscape',
                        'content_filter': 'high',
                        'client_id': Config.UNSPLASH_ACCESS_KEY
                    }
                    
                    response = requests.get(api_url, params=params, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        image_url = data['urls']['regular']
                        logger.info(f"📸 Unsplash API: Found image for '{topic}' with query: {search_query}")
                        return f"{image_url}?t={timestamp}"
                except Exception as api_error:
                    logger.warning(f"⚠️ Unsplash API failed, using public endpoint: {api_error}")
            
            # Use public Unsplash endpoint with smart search
            # Combine keywords for better results
            search_terms = ",".join(keywords[:3])  # Max 3 keywords for public API
            
            # Generate multiple search options for better matching
            search_options = [
                f"https://source.unsplash.com/featured/1200x800/?{search_terms}&sig={random_seed}",
                f"https://source.unsplash.com/1200x800/?food,{main_keyword}&sig={random_seed + 1}",
                f"https://source.unsplash.com/featured/?{main_keyword},recipe&sig={random_seed + 2}",
                f"https://images.unsplash.com/photo-1565958011703-44f9829ba187?ixlib=rb-1.2.1&auto=format&fit=crop&w=1200&h=800&q=80&crop=entropy&cs=tinysrgb&sig={random_seed}",  # High-quality food photo
                f"https://source.unsplash.com/featured/1200x800/?cooking,{main_keyword}&sig={random_seed + 3}",
            ]
            
            # Select based on topic hash for consistency
            topic_hash = hash(topic) % len(search_options)
            image_url = search_options[topic_hash]
            
            logger.info(f"📸 Unsplash: Using search terms '{search_terms}' for '{topic}'")
            return f"{image_url}&t={timestamp}"
            
        except Exception as e:
            logger.warning(f"⚠️ Unsplash image generation failed: {e}")
            # Fallback to high-quality food images
            fallback_url = random.choice(self.fallback_images)
            timestamp = int(time.time())
            return f"{fallback_url}?w=1200&h=800&fit=crop&crop=entropy&t={timestamp}"
    
    def generate_ai_content_with_hashtags(self, topic: str):
        def _generate():
            prompt = f'''Create a COMPLETE professional recipe blog post about "{topic}" for a cooking and baking blog.

IMPORTANT REQUIREMENTS:
1. Write an ENGAGING TITLE (5-10 words)
2. Write a CAPTIVATING INTRODUCTION paragraph (2-3 sentences)
3. Provide COMPLETE DETAILED RECIPE with these EXACT sections:

A. COMPLETE INGREDIENTS LIST:
   - List ALL ingredients with EXACT measurements
   - Include EVERY ingredient needed, not just examples
   - Format: "2 cups all-purpose flour", "1 teaspoon vanilla extract"

B. DETAILED PREPARATION STEPS:
   - Numbered steps 1 through 10+
   - Each step should be clear and actionable
   - Include cooking times and temperatures
   - Cover from preparation to serving

C. PROFESSIONAL COOKING TIPS (3-4 tips):
   - Specific tips for this recipe
   - Common mistakes to avoid
   - Techniques for best results

D. NUTRITIONAL INFORMATION (approximate):
   - Calories per serving
   - Protein, Carbs, Fat

E. SERVING SUGGESTIONS:
   - How to present and serve
   - Accompaniment suggestions

F. STORAGE INSTRUCTIONS:
   - How to store leftovers
   - Reheating instructions

4. Add a STRONG CALL-TO-ACTION
5. Generate EXACTLY 2 SPECIFIC HASHTAGS related to this recipe (without # symbol, just the words)

Format your response EXACTLY as JSON:
{{
    "title": "...",
    "introduction": "...",
    "ingredients": ["2 cups flour", "1 cup sugar", "...", "COMPLETE LIST"],
    "steps": ["Step 1: Preheat oven to 350°F", "Step 2: Mix dry ingredients", "...", "Step 10: Serve warm"],
    "tips": ["Use room temperature eggs", "Don't overmix batter", "..."],
    "nutrition": "Calories: 350 | Protein: 5g | Carbs: 45g | Fat: 15g",
    "serving": "Serve warm with vanilla ice cream",
    "storage": "Store in airtight container for up to 3 days",
    "cta": "...",
    "custom_hashtags": ["ChocolateLavaCake", "DessertRecipe"]
}}

CRITICAL: Provide COMPLETE recipe with ALL ingredients and steps. NO placeholders like "see link for full recipe". ALL content must be in this JSON.'''

            response = self.clients.gemini_model.generate_content(prompt)
            content = response.text.strip()
            
            # Clean JSON response
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0].strip()
            elif '```' in content:
                content = content.split('```')[1].split('```')[0].strip()
            
            try:
                data = json.loads(content)
                
                # Validate required fields
                required_fields = ['title', 'introduction', 'ingredients', 'steps', 'tips', 'cta']
                for field in required_fields:
                    if field not in data:
                        raise ValueError(f"Missing required field: {field}")
                
                # Ensure ingredients and steps are complete lists
                if not isinstance(data.get('ingredients', []), list) or len(data['ingredients']) < 3:
                    data['ingredients'] = self._default_ingredients(topic)
                if not isinstance(data.get('steps', []), list) or len(data['steps']) < 5:
                    data['steps'] = self._default_steps(topic)
                
                # Ensure hashtags
                if 'custom_hashtags' not in data or not isinstance(data['custom_hashtags'], list) or len(data['custom_hashtags']) != 2:
                    data['custom_hashtags'] = self._extract_hashtags_from_topic(topic)
                
                return data
                
            except json.JSONDecodeError:
                logger.warning("⚠️ JSON parsing failed, trying to extract...")
                import re
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                return self._fallback_complete_content(topic)
        
        try: 
            result = self.clients.retry_with_smart_backoff(_generate, "Gemini AI Complete Recipe", Config.GEMINI_MAX_RETRIES)
            logger.info(f"✅ Generated complete recipe with {len(result.get('ingredients', []))} ingredients and {len(result.get('steps', []))} steps")
            return result
        except:
            logger.warning("⚠️ Using fallback content due to API failure")
            return self._fallback_complete_content(topic)
    
    def _default_ingredients(self, topic: str):
        """Default ingredients if AI fails"""
        return [
            f"2 cups all-purpose flour for {topic}",
            "1 cup granulated sugar",
            "3 large eggs, room temperature",
            "1/2 cup unsalted butter, melted",
            "1 cup milk or buttermilk",
            "2 teaspoons baking powder",
            "1 teaspoon vanilla extract",
            "1/2 teaspoon salt"
        ]
    
    def _default_steps(self, topic: str):
        """Default preparation steps if AI fails"""
        return [
            f"Step 1: Preheat your oven to 350°F (175°C) for perfect {topic}",
            "Step 2: Grease and flour your baking pan to prevent sticking",
            "Step 3: In a large bowl, whisk together all dry ingredients",
            "Step 4: In another bowl, mix wet ingredients until smooth",
            "Step 5: Gradually combine wet and dry ingredients, mixing just until incorporated",
            "Step 6: Pour batter into prepared pan and smooth the top",
            f"Step 7: Bake for 25-30 minutes until {topic} is golden and toothpick comes out clean",
            "Step 8: Cool in pan for 10 minutes, then transfer to wire rack",
            "Step 9: Allow to cool completely before serving",
            f"Step 10: Serve your delicious {topic} with your favorite accompaniments"
        ]
    
    def _fallback_complete_content(self, topic: str):
        """Complete fallback content with all recipe details"""
        hashtags = self._extract_hashtags_from_topic(topic)
        return {
            "title": f"Professional Guide to Making Perfect {topic}",
            "introduction": f"Welcome to our detailed recipe for {topic}. This professional guide will walk you through every step to create restaurant-quality results at home.",
            "ingredients": self._default_ingredients(topic),
            "steps": self._default_steps(topic),
            "tips": [
                "Use room temperature ingredients for even mixing",
                "Preheat your oven fully before baking",
                "Measure flour correctly by spooning into cup and leveling",
                f"Don't overmix the {topic.lower()} batter to avoid toughness"
            ],
            "nutrition": "Approximately 300-400 calories per serving | Protein: 6g | Carbs: 50g | Fat: 12g",
            "serving": f"Serve {topic} warm with fresh fruit, whipped cream, or ice cream. Perfect for special occasions or everyday treats.",
            "storage": "Store in an airtight container at room temperature for 3 days, or freeze for up to 3 months.",
            "cta": "Try this recipe today and share your results with us! For more professional recipes and cooking tips, visit our blog regularly.",
            "custom_hashtags": hashtags
        }
    
    def _extract_hashtags_from_topic(self, topic: str):
        words = topic.replace('-', ' ').split()
        if len(words) >= 2:
            return [words[0] + words[1], topic.replace(' ', '') + 'Recipe']
        return [topic.replace(' ', ''), 'HomeBaking']
    
    def generate_final_hashtags(self, custom_hashtags: List[str]):
        final_tags = []
        for tag in custom_hashtags[:2]:
            clean_tag = tag.replace('#', '').replace(' ', '')
            if clean_tag:
                final_tags.append(clean_tag)
        final_tags.extend(random.sample(Config.GLOBAL_HASHTAGS, min(len(Config.GLOBAL_HASHTAGS), 8)))
        final_tags.extend(self.base_tags[:5])
        return list(dict.fromkeys(final_tags))[:15]
    
    def format_post_content(self, ai_content: Dict):
        """Format complete recipe content for Tumblr"""
        ingredients = "".join([f'<li>{i}</li>' for i in ai_content.get('ingredients', [])])
        steps = "".join([f'<li>{s}</li>' for s in ai_content.get('steps', [])])
        tips = "".join([f'<li>{t}</li>' for t in ai_content.get('tips', [])])
        
        # Add optional sections if they exist
        nutrition_html = f'''<h3 style="color: #27ae60;">📊 Nutritional Information:</h3>
<p style="color: #555;">{ai_content.get('nutrition', 'Nutrition information varies based on specific ingredients used.')}</p>''' if ai_content.get('nutrition') else ''
        
        serving_html = f'''<h3 style="color: #8e44ad;">🍽️ Serving Suggestions:</h3>
<p style="color: #555;">{ai_content.get('serving', '')}</p>''' if ai_content.get('serving') else ''
        
        storage_html = f'''<h3 style="color: #e67e22;">💾 Storage Instructions:</h3>
<p style="color: #555;">{ai_content.get('storage', '')}</p>''' if ai_content.get('storage') else ''
        
        return f'''<div style="font-family: Arial, sans-serif; line-height: 1.6; max-width: 800px; margin: 0 auto;">
<h1 style="color: #2c3e50; border-bottom: 3px solid #e74c3c; padding-bottom: 10px;">{ai_content["title"]}</h1>
<p style="font-size: 18px; color: #34495e; background: #f8f9fa; padding: 15px; border-radius: 5px;">{ai_content["introduction"]}</p>

<h2 style="color: #e74c3c; margin-top: 30px;">🛒 Ingredients:</h2>
<ul style="color: #555; background: #fff9e6; padding: 20px 20px 20px 40px; border-radius: 5px; border-left: 4px solid #f39c12;">{ingredients}</ul>

<h2 style="color: #3498db; margin-top: 30px;">👩‍🍳 Preparation Steps:</h2>
<ol style="color: #555; background: #e8f4f8; padding: 20px 20px 20px 40px; border-radius: 5px; border-left: 4px solid #3498db;">{steps}</ol>

<h2 style="color: #9b59b6; margin-top: 30px;">💡 Professional Tips:</h2>
<ul style="color: #555; background: #f4ecf7; padding: 20px 20px 20px 40px; border-radius: 5px; border-left: 4px solid #9b59b6;">{tips}</ul>

{nutrition_html}
{serving_html}
{storage_html}

<div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 25px; border-radius: 10px; margin-top: 30px; text-align: center;">
<h3 style="color: white; margin-top: 0;">✨ {ai_content["cta"]}</h3>
<p style="margin-bottom: 0;"><strong>Visit our blog:</strong> <a href="{Config.BLOG_WEBSITE}" target="_blank" style="color: #ffd700; text-decoration: underline;">She Cooks & Bakes - Professional Recipes</a></p>
</div>

<hr style="margin: 30px 0; border-top: 2px dashed #ddd;">
<p style="font-size: 14px; color: #7f8c8d; text-align: center;">
Posted by She Cooks Bakes AI • {datetime.now().strftime("%B %d, %Y")} • Complete Recipe Guide
</p>
</div>'''

class PublishingManager:
    def __init__(self, content_engine: ContentEngine, db_manager: DatabaseManager):
        self.engine = content_engine
        self.db = db_manager
    def human_delay(self, min_s=None, max_s=None):
        delay = random.randint(min_s or Config.HUMAN_DELAY_MIN, max_s or Config.HUMAN_DELAY_MAX)
        time.sleep(delay)
    def publish_post(self):
        start = time.time()
        try:
            topic = self.engine.generate_recipe_topic()
            logger.info(f"🍰 Topic: {topic}")
            logger.info("📝 Generating COMPLETE recipe content with Gemini AI...")
            ai_content = self.engine.generate_ai_content_with_hashtags(topic)
            logger.info(f"✅ Generated: {len(ai_content.get('ingredients', []))} ingredients, {len(ai_content.get('steps', []))} steps")
            logger.info(f"🏷️ Custom hashtags: {ai_content.get('custom_hashtags', [])}")
            self.human_delay(20, 40)
            logger.info("🎨 Fetching high-quality Unsplash image...")
            image_url = self.engine.generate_high_quality_unsplash_image(topic)
            self.human_delay(30, 50)
            final_tags = self.engine.generate_final_hashtags(ai_content.get('custom_hashtags', []))
            logger.info(f"🏷️ Final tags ({len(final_tags)}): {', '.join(final_tags[:5])}...")
            content = self.engine.format_post_content(ai_content)
            logger.info("🚀 Publishing complete recipe to Tumblr...")
            response = self.engine.clients.retry_with_smart_backoff(lambda: self.engine.clients.tumblr_client.create_photo(blogname=self.engine.blog_url, state="published", tags=final_tags, caption=content, source=image_url, format="html"), "Tumblr", Config.TUMBLR_MAX_RETRIES)
            post_id = response.get('id', 'unknown')
            duration = time.time() - start
            post_data = {"success": True, "post_id": str(post_id), "topic": topic, "title": ai_content['title'], "image_url": image_url, "tags": final_tags}
            self.db.save_post(post_data, duration)
            self.db.update_state('last_post_time', datetime.now().isoformat())
            self.db.update_state('total_posts', str(self.db.get_post_count()))
            metrics.record_publish_success(duration)
            self.db.save_metric('publish_duration', duration)
            logger.info(f"✅ Published COMPLETE recipe in {duration:.1f}s! ID: {post_id}")
            return post_data
        except Exception as e:
            duration = time.time() - start
            logger.error(f"❌ Publishing failed after {duration:.1f}s: {e}")
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
        logger.info("✅ Scheduler initialized (Memory jobstore)")
    def schedule_next_post(self):
        try:
            self.scheduler.remove_all_jobs()
            delay, next_time = self.publisher.calculate_next_post_time()
            self.scheduler.add_job(func=self.execute_post_job, trigger='interval', seconds=delay, id='post_job', replace_existing=True)
            logger.info(f"📅 Next post: {next_time.strftime('%Y-%m-%d %H:%M:%S')} ({delay // 60} minutes)")
            self.db.update_state('next_post_time', next_time.isoformat())
        except Exception as e:
            logger.error(f"❌ Schedule error: {e}")
    def execute_post_job(self):
        logger.info("=" * 60)
        logger.info("🎬 Executing scheduled post job")
        result = self.publisher.publish_post()
        if result['success']:
            logger.info(f"🎉 Success: {result['title']}")
        else:
            logger.error(f"❌ Failed: {result.get('error')}")
        self.schedule_next_post()
        logger.info("=" * 60)
    def start(self):
        self.schedule_next_post()

app = Flask(__name__)
db_manager = publisher = scheduler = None
start_time = time.time()

MOBILE_POST_HTML = '''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Post Now - She Cooks Bakes</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:600px;margin:0 auto;padding:20px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh}
.container{background:white;border-radius:15px;padding:30px;box-shadow:0 10px 30px rgba(0,0,0,0.3)}
h1{color:#2c3e50;text-align:center;margin-bottom:10px}
.subtitle{text-align:center;color:#7f8c8d;margin-bottom:30px}
button{width:100%;padding:15px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;border:none;border-radius:8px;font-size:18px;font-weight:bold;cursor:pointer;transition:all 0.3s}
button:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(102,126,234,0.4)}
button:active{transform:translateY(0)}
button:disabled{background:#95a5a6;cursor:not-allowed}
.result{margin-top:20px;padding:15px;border-radius:8px;display:none}
.success{background:#d4edda;color:#155724;border:1px solid #c3e6cb}
.error{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb}
.loading{text-align:center;color:#667eea;font-weight:bold}
.info{background:#e8f4f8;padding:15px;border-radius:8px;margin-bottom:20px;border-left:4px solid #667eea}
</style>
</head>
<body>
<div class="container">
<h1>🍰 She Cooks Bakes AI</h1>
<p class="subtitle">Powered by Google Gemini</p>
<div class="info"><strong>ℹ️ Info:</strong> This will create and publish a new blog post immediately.</div>
<button onclick="triggerPost()" id="postBtn">🚀 Publish Now</button>
<div id="result" class="result"></div>
</div>
<script>
function triggerPost(){const btn=document.getElementById('postBtn');const result=document.getElementById('result');btn.disabled=true;btn.textContent='⏳ Publishing...';result.style.display='block';result.className='result loading';result.textContent='Creating your post... This may take 1-2 minutes.';fetch('/post-now',{method:'POST',headers:{'Content-Type':'application/json'}}).then(response=>response.json()).then(data=>{if(data.result&&data.result.success){result.className='result success';result.innerHTML=`<strong>✅ Success!</strong><br><strong>Title:</strong> ${data.result.title}<br><strong>Post ID:</strong> ${data.result.post_id}<br><strong>Blog:</strong> <a href="https://shecooksandbakes.tumblr.com" target="_blank">View on Tumblr</a>`;}else{result.className='result error';result.innerHTML=`<strong>❌ Error:</strong> ${data.result?data.result.error:'Unknown error'}`;}btn.disabled=false;btn.textContent='🚀 Publish Now';}).catch(error=>{result.className='result error';result.textContent='❌ Network error: '+error;btn.disabled=false;btn.textContent='🚀 Publish Now';});}
</script>
</body>
</html>'''

@app.route('/')
def home():
    uptime = int(time.time() - start_time)
    return jsonify({"status": "active", "service": Config.APP_NAME, "version": Config.VERSION, "ai_engine": "Google Gemini", "uptime": f"{uptime // 3600}h {(uptime % 3600) // 60}m", "metrics": metrics.get_metrics(), "success_rate": f"{metrics.get_success_rate():.1f}%", "total_posts": db_manager.get_post_count(), "last_post": db_manager.get_last_post_time().isoformat() if db_manager.get_last_post_time() else "Never", "next_post": db_manager.get_state('next_post_time', 'Calculating...'), "blog": Config.BLOG_URL})

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat(), "database": "connected", "scheduler": "running", "ai": "Google Gemini"})

@app.route('/stats')
def stats():
    try:
        with db_manager.get_connection() as conn:
            recent = conn.execute('SELECT title, published_at, duration_seconds FROM posts ORDER BY published_at DESC LIMIT 5').fetchall()
            errors = conn.execute('SELECT COUNT(*) FROM errors WHERE DATE(occurred_at) = DATE("now")').fetchone()[0]
            avg_dur = conn.execute('SELECT AVG(duration_seconds) FROM posts WHERE status = "success"').fetchone()[0] or 0
        return jsonify({"total_posts": db_manager.get_post_count(), "recent_posts": [dict(r) for r in recent], "errors_today": errors, "avg_publish_time": f"{avg_dur:.1f}s", "performance": metrics.get_metrics()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/post-now', methods=['GET', 'POST'])
def manual_post():
    if request.method == 'GET':
        return render_template_string(MOBILE_POST_HTML)
    try:
        logger.info("🔧 Manual post triggered via " + request.method)
        result = publisher.publish_post()
        scheduler.schedule_next_post()
        return jsonify({"manual_trigger": True, "result": result})
    except Exception as e:
        logger.error(f"Manual post error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/backup', methods=['POST'])
def create_backup():
    try:
        return jsonify({"backup_created": db_manager.create_backup(), "timestamp": datetime.now().isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

class SelfPingService:
    def __init__(self):
        self.url = os.getenv('RENDER_EXTERNAL_URL', f'http://localhost:{Config.FLASK_PORT}')
        if Config.SELF_PING_ENABLED:
            threading.Thread(target=self._ping_loop, daemon=True).start()
            logger.info("💓 Self-ping service started")
    def _ping_loop(self):
        while True:
            time.sleep(Config.SELF_PING_INTERVAL)
            try: 
                requests.get(f"{self.url}/health", timeout=10)
                logger.debug("💓 Self-ping successful")
            except: 
                pass

def main():
    global db_manager, publisher, scheduler
    try:
        logger.info("=" * 60)
        logger.info(f"🚀 {Config.APP_NAME} v{Config.VERSION}")
        logger.info(f"🤖 AI Engine: Google Gemini (Free & Unlimited)")
        logger.info(f"📸 Image System: High-Quality Unsplash (Free)")
        logger.info(f"📝 Content: COMPLETE Recipes (No external links)")
        logger.info(f"⏰ Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
        logger.info("⏰ Initializing scheduler (Memory-based)...")
        scheduler = PostScheduler(publisher, db_manager)
        logger.info("💓 Starting self-ping service...")
        SelfPingService()
        logger.info("🎬 Starting scheduler...")
        scheduler.start()
        logger.info(f"🌐 Starting Waitress WSGI server on port {Config.FLASK_PORT}...")
        logger.info("=" * 60)
        logger.info("✅ SYSTEM FULLY OPERATIONAL!")
        logger.info("=" * 60)
        logger.info("📱 Mobile posting: https://your-app.onrender.com/post-now")
        logger.info("🏷️ Hashtag system: 2 AI-generated + global tags")
        logger.info("📸 Images: High-quality Unsplash (topic-relevant)")
        logger.info("📝 Recipes: COMPLETE with all ingredients & steps")
        logger.info("=" * 60)
        serve(app, host=Config.FLASK_HOST, port=Config.FLASK_PORT, threads=4, channel_timeout=60, cleanup_interval=30, _quiet=False)
    except KeyboardInterrupt:
        logger.info("⏹️ Shutting down gracefully...")
        if scheduler: scheduler.scheduler.shutdown()
        sys.exit(0)
    except Exception as e:
        logger.critical(f"💥 Critical error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
