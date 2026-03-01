from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import os
import zipfile
import subprocess
import signal
import shutil
import time
import secrets
import logging
from threading import Thread
from pathlib import Path
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from functools import wraps
import socket
import json
from collections import defaultdict
import sys
import pkg_resources
import stat

# Try to import optional dependencies
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[WARNING] psutil not installed. System stats will be limited.")

try:
    import humanize
    HUMANIZE_AVAILABLE = True
except ImportError:
    HUMANIZE_AVAILABLE = False
    print("[WARNING] humanize not installed. File sizes will be in bytes.")

app = Flask(__name__)
app.secret_key = "x9k7m3p5_2025_secure_key_!@#$"
app.config['MAX_CONTENT_LENGTH'] = 250 * 1024 * 1024
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# Configuration
UPLOAD_FOLDER = "codex_deployments"
MAX_RUNNING = 3  # Maximum concurrent running apps per user
MAX_UPLOADS_PER_USER = 3  # Maximum uploads per user
PORT = 8030
HOST = "0.0.0.0"

# Create necessary directories
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("logs", exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs("analytics_data", exist_ok=True)

# Logging setup
logging.basicConfig(
    filename='logs/codex.log',
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)
logger = logging.getLogger(__name__)

# Process tracking
processes = {}
app_logs = {}
app_venvs = {}  # Track virtual environments per app

# Admin credentials (HIDDEN - not displayed anywhere)
ADMIN_KEY = "X9K7M3P5"  # This is the secret admin key - keep hidden!

# Analytics tracking
visitors = []
page_views = 0
total_uploads = 0
total_file_size = 0
start_time = datetime.now()
user_upload_counts = defaultdict(int)  # Track uploads per user
user_sessions = {}  # Track active sessions

# File to store analytics data
ANALYTICS_FILE = "analytics_data/visitors.json"

# Load existing analytics if available
def load_analytics():
    global visitors, page_views, total_uploads, total_file_size, user_upload_counts
    try:
        if os.path.exists(ANALYTICS_FILE):
            with open(ANALYTICS_FILE, 'r') as f:
                data = json.load(f)
                visitors = [{'ip': v['ip'], 
                           'time': datetime.fromisoformat(v['time']), 
                           'user_agent': v['user_agent'],
                           'session_id': v.get('session_id', ''),
                           'username': v.get('username', 'Anonymous')} for v in data.get('visitors', [])]
                page_views = data.get('page_views', 0)
                total_uploads = data.get('total_uploads', 0)
                total_file_size = data.get('total_file_size', 0)
                user_upload_counts = defaultdict(int, data.get('user_upload_counts', {}))
    except Exception as e:
        logger.error(f"Failed to load analytics: {e}")

# Save analytics data
def save_analytics():
    try:
        data = {
            'visitors': [{'ip': v['ip'], 
                         'time': v['time'].isoformat(), 
                         'user_agent': v['user_agent'],
                         'session_id': v.get('session_id', ''),
                         'username': v.get('username', 'Anonymous')} for v in visitors[-1000:]],  # Keep last 1000
            'page_views': page_views,
            'total_uploads': total_uploads,
            'total_file_size': total_file_size,
            'user_upload_counts': dict(user_upload_counts)
        }
        with open(ANALYTICS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Failed to save analytics: {e}")

# Load analytics on startup
load_analytics()

# Get local IP address
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

LOCAL_IP = get_local_ip()

# ---------- Helper Functions ----------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session or not session.get('is_admin', False):
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def get_user_dir():
    user_dir = os.path.join(UPLOAD_FOLDER, secure_filename(session['username']))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def log_message(log_path, level, message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    try:
        with open(log_path, "a", encoding='utf-8') as f:
            f.write(f"[{timestamp}] [{level}] {message}\n")
    except:
        pass

def extract_zip(zip_path, extract_to):
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(extract_to)
        return True
    except Exception as e:
        logger.error(f"Extraction error: {e}")
        return False

def find_main_file(path):
    for f in ["main.py", "app.py", "application.py", "server.py", "bot.py", "run.py", "codex.py", "exploit.py", "index.py", "start.py"]:
        if os.path.exists(os.path.join(path, f)):
            return f
    return None

def create_virtual_env(app_dir):
    """Create a virtual environment for the app with proper error handling"""
    venv_dir = os.path.join(app_dir, "venv")
    
    # Remove existing venv if it's corrupted
    if os.path.exists(venv_dir):
        try:
            shutil.rmtree(venv_dir)
        except:
            pass
    
    try:
        log_message(os.path.join(app_dir, "logs.txt"), "VENV", "Creating virtual environment...")
        
        # Use venv module to create virtual environment
        import venv
        venv.create(venv_dir, with_pip=True, clear=True)
        
        # Wait a moment for the environment to be fully created
        time.sleep(2)
        
        # Verify the virtual environment was created successfully
        if os.path.exists(venv_dir):
            # Check for python executable
            python_exe = get_venv_python(venv_dir)
            if os.path.exists(python_exe):
                log_message(os.path.join(app_dir, "logs.txt"), "VENV", f"Virtual environment created at {venv_dir}")
                
                # Upgrade pip in the virtual environment
                try:
                    subprocess.run(
                        [python_exe, "-m", "pip", "install", "--upgrade", "pip"],
                        capture_output=True,
                        timeout=60
                    )
                except:
                    pass
                
                return venv_dir
            else:
                log_message(os.path.join(app_dir, "logs.txt"), "ERROR", f"Python executable not found at {python_exe}")
                return None
        else:
            log_message(os.path.join(app_dir, "logs.txt"), "ERROR", "Virtual environment directory not created")
            return None
            
    except Exception as e:
        log_message(os.path.join(app_dir, "logs.txt"), "ERROR", f"Failed to create virtual environment: {str(e)}")
        logger.error(f"Virtual environment creation failed: {str(e)}")
        return None

def get_venv_python(venv_dir):
    """Get the path to python executable in virtual environment"""
    if os.name == 'nt':  # Windows
        return os.path.join(venv_dir, "Scripts", "python.exe")
    else:  # Unix/Linux/Mac
        return os.path.join(venv_dir, "bin", "python")

def get_venv_pip(venv_dir):
    """Get the path to pip executable in virtual environment"""
    if os.name == 'nt':  # Windows
        return os.path.join(venv_dir, "Scripts", "pip.exe")
    else:  # Unix/Linux/Mac
        return os.path.join(venv_dir, "bin", "pip")

def install_requirements(extract_dir, app_dir):
    """Install requirements from requirements.txt"""
    requirements_path = os.path.join(extract_dir, "requirements.txt")
    log_path = os.path.join(app_dir, "logs.txt")
    
    if not os.path.exists(requirements_path):
        log_message(log_path, "INFO", "No requirements.txt found")
        return True
    
    try:
        log_message(log_path, "INSTALL", "Installing dependencies from requirements.txt...")
        
        # Read requirements file to show what will be installed
        with open(requirements_path, 'r') as f:
            requirements = f.read().strip().split('\n')
            log_message(log_path, "REQUIREMENTS", f"Packages to install: {', '.join([r for r in requirements if r and not r.startswith('#')])}")
        
        # Try to install using system pip first (most reliable)
        try:
            log_message(log_path, "INSTALL", "Attempting installation with system pip...")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", requirements_path],
                cwd=extract_dir,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode == 0:
                log_message(log_path, "SUCCESS", "All dependencies installed successfully with system pip")
                
                # Log installed packages
                for line in result.stdout.split('\n'):
                    if 'Successfully installed' in line:
                        log_message(log_path, "PACKAGES", line)
                return True
            else:
                log_message(log_path, "WARNING", f"System pip failed: {result.stderr[:200]}")
                log_message(log_path, "INSTALL", "Falling back to user installation...")
                
                # Try with --user flag
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--user", "-r", requirements_path],
                    cwd=extract_dir,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                
                if result.returncode == 0:
                    log_message(log_path, "SUCCESS", "Dependencies installed with --user flag")
                    return True
                else:
                    log_message(log_path, "ERROR", f"All installation attempts failed")
                    return False
                    
        except subprocess.TimeoutExpired:
            log_message(log_path, "ERROR", "Dependency installation timed out")
            return False
            
    except Exception as e:
        log_message(log_path, "ERROR", f"Error installing dependencies: {str(e)}")
        logger.error(f"Dependency installation error: {str(e)}")
        return False

def start_app(app_name):
    username = session['username']
    user_dir = get_user_dir()
    app_dir = os.path.join(user_dir, app_name)
    extract_dir = os.path.join(app_dir, "extracted")
    log_path = os.path.join(app_dir, "logs.txt")
    
    # Extract if needed
    if not os.path.exists(extract_dir):
        zip_path = os.path.join(app_dir, "app.zip")
        if os.path.exists(zip_path):
            log_message(log_path, "EXTRACT", "Extracting package...")
            if not extract_zip(zip_path, extract_dir):
                log_message(log_path, "ERROR", "Failed to extract zip")
                return False
    
    # Find main file
    main_file = find_main_file(extract_dir)
    if not main_file:
        log_message(log_path, "FAIL", "No main file found (main.py/app.py/bot.py/etc)")
        return False
    
    # Check if already running
    key = (username, app_name)
    if key in processes:
        if processes[key].poll() is None:
            log_message(log_path, "INFO", "Application already running")
            return True
        processes.pop(key, None)
    
    try:
        log_file = open(log_path, "a", encoding='utf-8')
        log_message(log_path, "EXEC", f"Launching {main_file}")
        
        # Use system Python (most reliable)
        python_cmd = sys.executable
        log_message(log_path, "INFO", f"Using Python: {python_cmd}")
        
        # Check if requirements exist and try to install them
        requirements_path = os.path.join(extract_dir, "requirements.txt")
        if os.path.exists(requirements_path):
            log_message(log_path, "REQUIREMENTS", "Found requirements.txt")
            install_requirements(extract_dir, app_dir)
        
        # Start process
        process = subprocess.Popen(
            [python_cmd, main_file],
            cwd=extract_dir,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        processes[key] = process
        log_message(log_path, "START", f"Process started with PID: {process.pid}")
        
        # Wait a moment to check for immediate failure
        time.sleep(2)
        if process.poll() is not None:
            log_message(log_path, "FAIL", f"Process exited immediately with code {process.returncode}")
            processes.pop(key, None)
            return False
        
        return True
        
    except Exception as e:
        log_message(log_path, "ERROR", f"Failed to start: {str(e)}")
        logger.error(f"Start failed for {username}/{app_name}: {str(e)}")
        return False

def stop_app(app_name):
    key = (session['username'], app_name)
    if key in processes:
        try:
            # Try graceful termination
            processes[key].terminate()
            processes[key].wait(timeout=5)
            log_message(os.path.join(get_user_dir(), app_name, "logs.txt"), 
                       "STOP", "Process terminated gracefully")
        except:
            try:
                # Force kill if graceful fails
                os.killpg(os.getpgid(processes[key].pid), signal.SIGKILL)
                log_message(os.path.join(get_user_dir(), app_name, "logs.txt"), 
                           "KILL", "Process force killed")
            except:
                pass
        finally:
            processes.pop(key, None)

def get_logs(app_name, max_lines=2000):
    log_path = os.path.join(get_user_dir(), app_name, "logs.txt")
    if not os.path.exists(log_path):
        return "[ SYSTEM ]: No logs available"
    
    try:
        with open(log_path, "r", encoding='utf-8') as f:
            lines = f.readlines()
            if len(lines) > max_lines:
                lines = lines[-max_lines:]
            return "".join(lines)
    except:
        return "[ ERROR ]: Cannot read logs"

def get_file_size_human(size):
    if HUMANIZE_AVAILABLE:
        return humanize.naturalsize(size)
    else:
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

def get_user_upload_count(username):
    """Get number of uploads for a specific user"""
    user_dir = os.path.join(UPLOAD_FOLDER, secure_filename(username))
    if not os.path.exists(user_dir):
        return 0
    return len([d for d in os.listdir(user_dir) if os.path.isdir(os.path.join(user_dir, d))])

def get_system_stats():
    """Get system analytics"""
    global page_views, total_uploads, total_file_size
    
    # Calculate total file size in uploads folder
    total_size = 0
    file_count = 0
    user_files = defaultdict(int)
    user_sizes = defaultdict(int)
    
    for root, dirs, files in os.walk(UPLOAD_FOLDER):
        for file in files:
            file_path = os.path.join(root, file)
            try:
                size = os.path.getsize(file_path)
                total_size += size
                file_count += 1
                
                # Get username from path
                parts = root.split(os.sep)
                if len(parts) > 1:
                    username = parts[1]
                    user_files[username] += 1
                    user_sizes[username] += size
            except:
                pass
    
    # Get visitor count (unique IPs in last 24h)
    now = datetime.now()
    unique_visitors_24h = len(set([v['ip'] for v in visitors if v['time'] > now - timedelta(hours=24)]))
    unique_visitors_7d = len(set([v['ip'] for v in visitors if v['time'] > now - timedelta(days=7)]))
    
    # Active users (last 5 minutes)
    active_users = len(set([v['username'] for v in visitors if v['time'] > now - timedelta(minutes=5) and v['username'] != 'Anonymous']))
    
    # System stats with fallbacks
    cpu_percent = 0
    memory_percent = 0
    disk_usage = 0
    
    if PSUTIL_AVAILABLE:
        try:
            cpu_percent = psutil.cpu_percent()
            memory_percent = psutil.virtual_memory().percent
            disk_usage = psutil.disk_usage('/').percent
        except:
            pass
    
    return {
        'total_visitors': len(visitors),
        'unique_visitors_24h': unique_visitors_24h,
        'unique_visitors_7d': unique_visitors_7d,
        'active_users_now': active_users,
        'page_views': page_views,
        'total_uploads': total_uploads,
        'total_files': file_count,
        'total_file_size': total_size,
        'total_file_size_human': get_file_size_human(total_size),
        'active_processes': len(processes),
        'uptime': str(datetime.now() - start_time).split('.')[0],
        'cpu_percent': cpu_percent,
        'memory_percent': memory_percent,
        'disk_usage': disk_usage,
        'host': HOST,
        'port': PORT,
        'local_ip': LOCAL_IP,
        'psutil_available': PSUTIL_AVAILABLE,
        'humanize_available': HUMANIZE_AVAILABLE,
        'user_files': dict(user_files),
        'user_sizes': {k: get_file_size_human(v) for k, v in user_sizes.items()},
        'max_uploads_per_user': MAX_UPLOADS_PER_USER
    }

def clear_all_data():
    """Clear all analytics and uploaded files (Admin only)"""
    global visitors, page_views, total_uploads, total_file_size, user_upload_counts, processes
    
    # Stop all processes
    for key in list(processes.keys()):
        try:
            processes[key].terminate()
        except:
            pass
    processes = {}
    
    # Clear uploaded files
    try:
        shutil.rmtree(UPLOAD_FOLDER)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    except:
        pass
    
    # Clear analytics
    visitors = []
    page_views = 0
    total_uploads = 0
    total_file_size = 0
    user_upload_counts = defaultdict(int)
    
    # Save cleared state
    save_analytics()
    
    logger.info(f"All data cleared by admin")

# ---------- Routes ----------
@app.route("/")
def index():
    global page_views
    page_views += 1
    
    # Track visitor with session
    visitor_ip = request.remote_addr
    session_id = session.get('session_id', secrets.token_hex(8))
    session['session_id'] = session_id
    
    visitors.append({
        'ip': visitor_ip,
        'time': datetime.now(),
        'user_agent': request.user_agent.string if request.user_agent else "Unknown",
        'session_id': session_id,
        'username': session.get('username', 'Anonymous')
    })
    
    # Keep only last 5000 visitors
    if len(visitors) > 5000:
        visitors[:] = visitors[-5000:]
    
    # Save periodically (every 10 visitors)
    if len(visitors) % 10 == 0:
        save_analytics()
    
    if 'username' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route("/login", methods=["GET", "POST"])
def login():
    global page_views
    page_views += 1
    
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        if username and len(username) >= 3:
            session.permanent = True
            session['username'] = secure_filename(username)
            session['login_time'] = datetime.now().isoformat()
            session['session_id'] = secrets.token_hex(8)
            logger.info(f"User login: {username}")
            
            # Update last visitor with username
            if visitors:
                visitors[-1]['username'] = username
            save_analytics()
            
            return redirect(url_for('dashboard'))
    
    return render_template("login.html")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    global page_views
    page_views += 1
    
    if request.method == "POST":
        admin_key = request.form.get("admin_key")
        # Hidden admin key - not displayed anywhere
        if admin_key == ADMIN_KEY:
            session.permanent = True
            session['username'] = "ADMIN"
            session['is_admin'] = True
            session['session_id'] = secrets.token_hex(8)
            logger.info(f"ADMIN ACCESS GRANTED from IP: {request.remote_addr}")
            
            # Update last visitor with admin
            if visitors:
                visitors[-1]['username'] = "ADMIN"
            save_analytics()
            
            return redirect(url_for('dashboard'))
        else:
            logger.warning(f"Failed admin attempt with key: {admin_key} from IP: {request.remote_addr}")
    
    return render_template("admin.html")

@app.route("/dashboard")
@login_required
def dashboard():
    global page_views
    page_views += 1
    
    user_dir = get_user_dir()
    apps = []
    upload_count = 0
    
    if os.path.exists(user_dir):
        for item in os.listdir(user_dir):
            app_path = os.path.join(user_dir, item)
            if os.path.isdir(app_path):
                upload_count += 1
                is_running = (session['username'], item) in processes
                if is_running:
                    proc = processes.get((session['username'], item))
                    if proc and proc.poll() is not None:
                        is_running = False
                        processes.pop((session['username'], item), None)
                
                # Get file size
                zip_path = os.path.join(app_path, "app.zip")
                file_size = 0
                if os.path.exists(zip_path):
                    file_size = os.path.getsize(zip_path)
                
                apps.append({
                    "name": item,
                    "running": is_running,
                    "logs": get_logs(item),
                    "size": get_file_size_human(file_size)
                })
    
    remaining_uploads = MAX_UPLOADS_PER_USER - upload_count
    
    return render_template("dashboard.html", 
                         apps=apps, 
                         username=session['username'],
                         max_apps=MAX_RUNNING,
                         max_uploads=MAX_UPLOADS_PER_USER,
                         upload_count=upload_count,
                         remaining_uploads=remaining_uploads,
                         is_admin=session.get('is_admin', False),
                         host=HOST,
                         port=PORT,
                         local_ip=LOCAL_IP)

@app.route("/analytics")
@admin_required
def analytics():
    """Analytics dashboard - only for admin"""
    stats = get_system_stats()
    
    # Get recent visitors (real data, not fake)
    recent_visitors = sorted(visitors[-100:], key=lambda x: x['time'], reverse=True)
    
    # Get user directory apps count
    user_dir = get_user_dir()
    apps = []
    if os.path.exists(user_dir):
        for item in os.listdir(user_dir):
            if os.path.isdir(os.path.join(user_dir, item)):
                apps.append(item)
    
    # Get upload statistics by user
    user_stats = []
    all_users = set()
    for v in visitors:
        if v['username'] != 'Anonymous':
            all_users.add(v['username'])
    
    for username in all_users:
        user_uploads = get_user_upload_count(username)
        user_stats.append({
            'username': username,
            'uploads': user_uploads,
            'max_uploads': MAX_UPLOADS_PER_USER,
            'remaining': MAX_UPLOADS_PER_USER - user_uploads
        })
    
    return render_template("analytics.html", 
                         stats=stats,
                         visitors=recent_visitors,
                         apps=apps,
                         max_apps=MAX_RUNNING,
                         user_stats=user_stats)

@app.route("/admin/clear-all", methods=["POST"])
@admin_required
def clear_all():
    """Clear all data (admin only)"""
    clear_all_data()
    return redirect(url_for('analytics'))

@app.route("/deploy", methods=["POST"])
@login_required
def deploy():
    global total_uploads, total_file_size
    
    if 'file' not in request.files:
        return redirect(url_for('dashboard'))
    
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.zip'):
        return redirect(url_for('dashboard'))
    
    # Check user upload limit
    username = session['username']
    current_uploads = get_user_upload_count(username)
    
    if current_uploads >= MAX_UPLOADS_PER_USER:
        return redirect(url_for('dashboard'))
    
    total_uploads += 1
    
    # Track file size
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    total_file_size += file_size
    
    user_dir = get_user_dir()
    base_name = secure_filename(file.filename.replace('.zip', ''))
    
    if not base_name:
        base_name = "package"
    
    app_name = base_name
    counter = 1
    while os.path.exists(os.path.join(user_dir, app_name)):
        app_name = f"{base_name}_{counter}"
        counter += 1
    
    app_dir = os.path.join(user_dir, app_name)
    os.makedirs(app_dir)
    
    zip_path = os.path.join(app_dir, "app.zip")
    file.save(zip_path)
    
    log_path = os.path.join(app_dir, "logs.txt")
    log_message(log_path, "UPLOAD", f"Deployed: {app_name} ({get_file_size_human(file_size)})")
    
    # Auto-extract on upload
    extract_dir = os.path.join(app_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    
    log_message(log_path, "EXTRACT", "Extracting package...")
    if extract_zip(zip_path, extract_dir):
        log_message(log_path, "EXTRACT", "Extraction complete")
        
        # Check for requirements.txt
        if os.path.exists(os.path.join(extract_dir, "requirements.txt")):
            log_message(log_path, "REQUIREMENTS", "requirements.txt found")
            # Install requirements immediately
            install_requirements(extract_dir, app_dir)
        else:
            log_message(log_path, "INFO", "No requirements.txt found")
    else:
        log_message(log_path, "ERROR", "Extraction failed")
    
    # Update user upload count
    user_upload_counts[username] = current_uploads + 1
    save_analytics()
    
    return redirect(url_for('dashboard'))

@app.route("/app/<name>/start")
@login_required
def start(name):
    user_apps = sum(1 for k in processes.keys() if k[0] == session['username'])
    if user_apps >= MAX_RUNNING:
        return redirect(url_for('dashboard'))
    
    start_app(name)
    return redirect(url_for('dashboard'))

@app.route("/app/<name>/stop")
@login_required
def stop(name):
    stop_app(name)
    return redirect(url_for('dashboard'))

@app.route("/app/<name>/restart")
@login_required
def restart(name):
    stop_app(name)
    time.sleep(1)
    start_app(name)
    return redirect(url_for('dashboard'))

@app.route("/app/<name>/delete")
@login_required
def delete(name):
    stop_app(name)
    
    app_dir = os.path.join(get_user_dir(), name)
    try:
        shutil.rmtree(app_dir)
    except:
        pass
    
    return redirect(url_for('dashboard'))

@app.route("/api/logs/<name>")
@login_required
def api_logs(name):
    return jsonify({"logs": get_logs(name)})

@app.route("/api/stats")
@admin_required
def api_stats():
    """API endpoint for real-time stats"""
    return jsonify(get_system_stats())

@app.route("/api/visitors/recent")
@admin_required
def api_recent_visitors():
    """API endpoint for recent visitors"""
    recent = [{'ip': v['ip'], 
               'time': v['time'].isoformat(), 
               'user_agent': v['user_agent'][:50],
               'username': v['username']} for v in visitors[-20:]]
    return jsonify(recent)

@app.route("/api/status")
def api_status():
    """Public status endpoint"""
    return jsonify({
        "status": "online",
        "time": datetime.now().isoformat(),
        "host": HOST,
        "port": PORT,
        "local_ip": LOCAL_IP,
        "visitors_today": len([v for v in visitors if v['time'] > datetime.now() - timedelta(days=1)])
    })

@app.route("/logout")
def logout():
    username = session.get('username', 'Unknown')
    logger.info(f"Logout: {username}")
    session.clear()
    return redirect(url_for('login'))

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal Server Error: {error}")
    return "Internal Server Error. Check logs for details.", 500

# Cleanup thread
def cleanup():
    while True:
        time.sleep(30)
        to_remove = []
        for key, proc in list(processes.items()):
            if proc.poll() is not None:
                to_remove.append(key)
        for key in to_remove:
            processes.pop(key, None)
            logger.info(f"Cleaned up dead process: {key}")
        
        # Save analytics periodically
        save_analytics()

# Start cleanup thread
cleanup_thread = Thread(target=cleanup, daemon=True)
cleanup_thread.start()

if __name__ == "__main__":
    print(f"""
    ╔══════════════════════════════════════════════════════════╗
    ║                   ᎬꪎՄ CODEX v2.0                        ║
    ╠══════════════════════════════════════════════════════════╣
    ║  ▶ Server is LIVE!                                        ║
    ║  ▶ Host: {HOST}                                           ║
    ║  ▶ Port: {PORT}                                           ║
    ║  ▶ Local Access: http://{LOCAL_IP}:{PORT}                 ║
    ║  ▶ Admin Access: HIDDEN                                   ║
    ║  ▶ Max Uploads/User: {MAX_UPLOADS_PER_USER}               ║
    ║  ▶ Max Active Apps: {MAX_RUNNING}                         ║
    ║  ▶ Auto-Install: requirements.txt (system pip)            ║
    ╠══════════════════════════════════════════════════════════╣
    ║  ▶ Press CTRL+C to stop server                           ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    
    try:
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n[!] Server stopped by user")
    except Exception as e:
        print(f"\n[!] Error starting server: {e}")
