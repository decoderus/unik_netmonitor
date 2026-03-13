# school_network_monitor.py
import os
import sys
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, messagebox, Listbox, END
import requests
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
import configparser

# === Логирование ошибок на старте ===
def log_error_to_file():
    import traceback
    with open("error.log", "w", encoding="utf-8") as f:
        f.write("=== CRASH REPORT ===\n")
        f.write(traceback.format_exc())

sys.excepthook = lambda exc_type, exc_value, exc_tb: (
    log_error_to_file() if not issubclass(exc_type, KeyboardInterrupt) else None
)
# ===================================

# === Файлы конфигурации ===
CONFIG_FILE = "config.ini"
KNOWN_FILE = "known_devices.json"
HISTORY_FILE = "devices_history.json"
ELK_LOG_FILE = "log.jsonl"

running = False
ROUTER_SESSION = None
ROUTER_STOK = None
ROUTER_ENABLED = False
ROUTER_TYPE = "tp-link"
ROUTER_IP = "192.168.0.1"

# === ELK-совместимое логирование ===
def elk_log(event_type, level, message, **extra):
    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = {
        "timestamp": timestamp,
        "level": level,
        "event_type": event_type,
        "message": message,
        **extra
    }
    try:
        with open(ELK_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def log_to_gui(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    root.after(0, lambda: log_listbox.insert(END, line))
    root.after(0, lambda: log_listbox.see(END))

def log_event(level, event_type, message, **extra):
    log_to_gui(message)
    elk_log(event_type, level, message, **extra)

# === Загрузка настроек из config.ini ===
def load_configs():
    global ROUTER_ENABLED, ROUTER_TYPE, ROUTER_IP
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        elk_log("config_missing", "WARNING", "Файл config.ini не найден")
        return None, None, False, False, "", "", "tp-link", "192.168.0.1"
    
    try:
        config.read(CONFIG_FILE, encoding='utf-8')
    except Exception as e:
        elk_log("config_error", "ERROR", f"Ошибка чтения config.ini: {e}")
        return None, None, False, False, "", "", "tp-link", "192.168.0.1"
    
    bot_token = config.get('telegram', 'bot_token', fallback='').strip()
    chat_id = config.get('telegram', 'chat_id', fallback='').strip()
    
    # Проверяем, существует ли секция [router]
    if 'router' not in config:
        # Секция отсутствует - режим роутера выключен
        elk_log("router_section_missing", "INFO", "Секция [router] отсутствует в config.ini")
        return bot_token, chat_id, True, False, "", "", "tp-link", "192.168.0.1"
    
    ROUTER_ENABLED = config.getboolean('router', 'enabled', fallback=False)
    if ROUTER_ENABLED:
        ROUTER_TYPE = config.get('router', 'type', fallback='tp-link').strip().lower()
        ROUTER_IP = config.get('router', 'ip', fallback='192.168.0.1').strip()
        username = config.get('router', 'username', fallback='').strip()
        password = config.get('router', 'password', fallback='').strip()
        
        # Проверка наличия пароля (только если режим включён)
        if not password:
            elk_log("router_auth_missing", "WARNING", "Режим роутера включён, но отсутствует пароль")
            log_event("WARNING", "router_auth_missing", "⚠️ Режим роутера включён, но не указан пароль")
            ROUTER_ENABLED = False
            username = password = ""
    else:
        username = password = ""
    
    return bot_token, chat_id, True, ROUTER_ENABLED, username, password, ROUTER_TYPE, ROUTER_IP

# === Работа с роутером TP-Link ===
def login_to_tplink(ip, username, password):
    global ROUTER_SESSION, ROUTER_STOK
    try:
        session = requests.Session()
        
        # Базовая аутентификация HTTP (Basic Auth)
        auth_url = f"http://{ip}/userRpm/LoginRpm.htm"
        
        # Если логин пустой — используем только пароль
        if not username:
            username = ""  # Пустой логин
        
        # Попытка авторизации через базовую аутентификацию
        resp = session.get(auth_url, auth=(username, password), timeout=5)
        
        if resp.status_code == 200:
            ROUTER_SESSION = session
            ROUTER_STOK = "tplink_session"
            log_event("INFO", "router_login", f"✅ Успешный вход в роутер TP-Link {ip}")
            return True
        
        # Если базовая аутентификация не сработала — пробуем через форму
        if resp.status_code in [401, 403]:
            login_data = {
                "username": username,
                "password": password
            }
            resp = session.post(f"http://{ip}/", data=login_data, timeout=5)
            
            if resp.status_code == 200:
                ROUTER_SESSION = session
                ROUTER_STOK = "tplink_session"
                log_event("INFO", "router_login", f"✅ Успешный вход в роутер TP-Link {ip}")
                return True
        
        log_event("ERROR", "router_login_failed", f"❌ Ошибка входа в роутер {ip}: неверный логин или пароль")
        return False
        
    except requests.exceptions.Timeout:
        log_event("ERROR", "router_timeout", f"❌ Таймаут подключения к роутеру {ip}")
        return False
    except requests.exceptions.ConnectionError:
        log_event("ERROR", "router_connection", f"❌ Не удалось подключиться к роутеру {ip}")
        return False
    except Exception as e:
        log_event("ERROR", "router_unknown_error", f"⚠️ Неизвестная ошибка при подключении к роутеру: {e}")
        return False

def get_devices_from_tplink():
    global ROUTER_SESSION, ROUTER_IP
    if not ROUTER_SESSION:
        return None
    try:
        # Получаем список подключённых устройств
        url = f"http://{ROUTER_IP}/data/connected_devices.json"
        resp = ROUTER_SESSION.get(url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            devices = {}
            for dev in data.get("connected_device", []):
                mac = dev.get("mac_addr", "").lower().replace("-", ":")
                ip = dev.get("ip_addr", "")
                name = dev.get("device_name", "—")
                if mac and ip:
                    devices[mac] = {"ip": ip, "name": name, "online": True}
            return devices
        else:
            log_event("WARNING", "router_api_error", "⚠️ Ошибка API роутера, переключение на ARP-режим")
            return None
    except Exception as e:
        log_event("ERROR", "router_api_exception", f"⚠️ Исключение при запросе к роутеру: {e}")
        return None

def block_device_tplink(mac, name=""):
    global ROUTER_SESSION, ROUTER_IP
    if not ROUTER_SESSION:
        return False
    try:
        # Добавление в чёрный список
        url = f"http://{ROUTER_IP}/data/mac_filter.json"
        payload = {
            "mac_filter": {
                "mac_addr": mac,
                "status": "on",
                "comment": name or "Blocked by SchoolMonitor"
            }
        }
        resp = ROUTER_SESSION.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            log_event("INFO", "device_blocked", f"✅ Устройство заблокировано: {mac}", mac=mac)
            return True
        log_event("ERROR", "block_failed", f"❌ Не удалось заблокировать {mac}")
        return False
    except Exception as e:
        log_event("ERROR", "block_exception", f"⚠️ Ошибка блокировки: {e}")
        return False

def unblock_device_tplink(mac):
    global ROUTER_SESSION, ROUTER_IP
    if not ROUTER_SESSION:
        return False
    try:
        url = f"http://{ROUTER_IP}/data/mac_filter.json"
        payload = {"mac_addr": mac, "action": "delete"}
        resp = ROUTER_SESSION.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            log_event("INFO", "device_unblocked", f"🔓 Устройство разблокировано: {mac}", mac=mac)
            return True
        log_event("ERROR", "unblock_failed", f"❌ Не удалось разблокировать {mac}")
        return False
    except Exception as e:
        log_event("ERROR", "unblock_exception", f"⚠️ Ошибка разблокировки: {e}")
        return False

# === Резервный режим: arp -a ===
def get_local_network_range():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()

        config = configparser.ConfigParser()
        if os.path.exists(CONFIG_FILE):
            config.read(CONFIG_FILE, encoding='utf-8')
            mask = config.get('network', 'subnet_mask', fallback='/24').strip()
            if not mask.startswith('/'):
                mask = '/24'
            try:
                prefix = int(mask[1:])
                if not (8 <= prefix <= 30):
                    mask = '/24'
            except:
                mask = '/24'
        else:
            mask = '/24'

        cidr = f"{local_ip}{mask}"
        ip_net = ipaddress.IPv4Network(cidr, strict=False)
        return [str(ip) for ip in ip_net.hosts()]
    except Exception as e:
        elk_log("network_error", "ERROR", f"Не удалось определить подсеть: {e}")
        return [f"192.168.1.{i}" for i in range(1, 255)]

def ping_host(ip):
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        result = subprocess.run(
            ['ping', '-n', '1', '-w', '200', ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    else:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', '1', ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    return result.returncode == 0

def ping_sweep():
    ips = get_local_network_range()
    with ThreadPoolExecutor(max_workers=100) as executor:
        executor.map(ping_host, ips)

def normalize_mac(mac):
    if not mac:
        return None
    mac = mac.lower().replace('-', ':').replace('.', ':')
    parts = mac.split(':')
    if len(parts) == 6:
        try:
            for p in parts:
                int(p, 16)
            return ':'.join(f"{int(p, 16):02x}" for p in parts)
        except ValueError:
            return None
    return None

def is_valid_device(ip, mac):
    if not ip or not mac:
        return False
    try:
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
            return False
    except ValueError:
        return False
    if mac.startswith(('00:00:00', 'ff:ff:ff')):
        return False
    return True

def get_devices_via_arp():
    ping_sweep()
    result = subprocess.run(['arp', '-a'], capture_output=True, text=True)
    devices = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ip = parts[0]
        if not ip.replace('.', '').isdigit():
            continue
        mac = None
        for part in parts:
            if ':' in part or '-' in part:
                mac = normalize_mac(part)
                if mac:
                    break
        if mac and is_valid_device(ip, mac):
            devices[mac] = {"ip": ip, "name": "—", "online": True}
    return devices

# === Telegram ===
def send_telegram_alert(message, mac=None, ip=None):
    bot_token, chat_id, ok = load_configs()[:3]
    if not ok:
        log_event("WARNING", "telegram_disabled", "Файл config.ini не найден. Уведомления недоступны.")
        return
    if not bot_token or not chat_id:
        log_event("WARNING", "telegram_incomplete", "В config.ini отсутствуют bot_token или chat_id")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"🚨 *Монитор школьной сети*\n{message}",
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.ok:
            log_event("INFO", "telegram_sent", "✅ Уведомление отправлено в Telegram", mac=mac, ip=ip)
        else:
            log_event("ERROR", "telegram_failed", f"❌ Ошибка Telegram: {response.status_code}")
    except Exception as e:
        log_event("ERROR", "telegram_exception", f"⚠️ Не удалось отправить в Telegram: {e}")

# === Работа с белым списком и историей ===
def load_known_devices():
    if os.path.exists(KNOWN_FILE):
        with open(KNOWN_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    return set()

def save_known_devices(devices):
    with open(KNOWN_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(devices), f, indent=2)

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

# === Сканирование сети ===
def scan_once():
    global ROUTER_ENABLED, ROUTER_SESSION, ROUTER_TYPE, ROUTER_IP

    bot_token, chat_id, cfg_ok, router_enabled, username, password, rtype, rip = load_configs()
    ROUTER_ENABLED = router_enabled
    ROUTER_TYPE = rtype
    ROUTER_IP = rip

    devices = {}
    router_mode_active = False
    
    if router_enabled:
        if not ROUTER_SESSION:
            if login_to_tplink(ROUTER_IP, username, password):
                router_mode_active = True
            else:
                # Автоматический откат на ARP-режим
                log_event("WARNING", "router_fallback", "🔄 Режим роутера недоступен, переключаюсь на ARP-режим")
                router_enabled = False
        if router_enabled:
            dev_router = get_devices_from_tplink()
            if dev_router is None:
                log_event("WARNING", "router_fallback", "🔄 API роутера недоступно, переключаюсь на ARP-режим")
                router_enabled = False
            else:
                devices = dev_router
                router_mode_active = True

    if not router_enabled:
        devices = get_devices_via_arp()

    history = load_history()
    known = load_known_devices()
    new_found = False

    for mac, info in devices.items():
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        history[mac] = {
            "ip": info["ip"],
            "last_seen": now,
            "name": info.get("name", "—"),
            "online": info.get("online", True)
        }
        if mac not in known:
            new_found = True
            msg = f"Новое устройство:\nMAC: {mac}\nIP: {info['ip']}\nИмя: {info.get('name', '—')}"
            log_event("INFO", "device_detected", f"Обнаружено новое устройство: {mac}", mac=mac, ip=info["ip"])
            send_telegram_alert(msg, mac=mac, ip=info["ip"])

    save_history(history)
    root.after(0, lambda: update_device_display(history, known))

    if not new_found:
        log_event("INFO", "scan_completed", "✅ Новых устройств не обнаружено")
    else:
        log_event("INFO", "scan_completed", f"✅ Обнаружено {new_found} новых устройств(а)")

# === Обновление таблицы устройств ===
def update_device_display(history, known_macs):
    try:
        device_tree.delete(*device_tree.get_children())
        for mac, info in history.items():
            status = "Известное" if mac in known_macs else "НОВОЕ"
            device_tree.insert(
                "", "end",
                values=(mac, info.get("ip", "—"), info.get("last_seen", "—"), status)
            )
    except Exception as e:
        log_event("ERROR", "gui_update_failed", f"Ошибка обновления таблицы: {e}")

# === Фоновый мониторинг ===
def background_scanner():
    global running
    while running:
        scan_once()
        time.sleep(60)

def start_background():
    global running
    if not running:
        running = True
        threading.Thread(target=background_scanner, daemon=True).start()
        start_btn.config(state='disabled')
        stop_btn.config(state='normal')
        log_event("INFO", "monitoring_started", "▶️ Фоновый мониторинг запущен")

def stop_background():
    global running
    running = False
    start_btn.config(state='normal')
    stop_btn.config(state='disabled')
    log_event("INFO", "monitoring_stopped", "⏹️ Фоновый мониторинг остановлен")

# === Управление белым списком ===
def add_selected_to_known():
    selection = device_tree.selection()
    if not selection:
        messagebox.showwarning("Внимание", "Выберите устройство в таблице")
        return
    item = device_tree.item(selection[0])
    mac = item["values"][0]
    known = load_known_devices()
    known.add(mac)
    save_known_devices(known)
    log_event("INFO", "device_whitelisted", f"✅ Устройство добавлено: {mac}", mac=mac)
    scan_once()

def remove_selected_from_known():
    selection = device_tree.selection()
    if not selection:
        messagebox.showwarning("Внимание", "Выберите устройство в таблице")
        return
    item = device_tree.item(selection[0])
    mac = item["values"][0]
    known = load_known_devices()
    if mac in known:
        known.discard(mac)
        save_known_devices(known)
        log_event("INFO", "device_removed", f"🗑️ Устройство удалено из белого списка: {mac}", mac=mac)
        scan_once()
    else:
        messagebox.showinfo("Информация", "Устройство не входит в белый список")

def add_all_to_known():
    history = load_history()
    if not history:
        messagebox.showinfo("Информация", "Нет устройств для добавления")
        return
    known = load_known_devices()
    new_count = 0
    for mac in history.keys():
        if mac not in known:
            known.add(mac)
            new_count += 1
    if new_count > 0:
        save_known_devices(known)
        log_event("INFO", "bulk_whitelist", f"✅ Добавлено {new_count} устройств в белый список")
    else:
        log_event("INFO", "bulk_whitelist", "ℹ️ Все устройства уже в белом списке")
    scan_once()

# === Блокировка/разблокировка через роутер ===
def block_selected():
    selection = device_tree.selection()
    if not selection:
        messagebox.showwarning("Внимание", "Выберите устройство в таблице")
        return
    item = device_tree.item(selection[0])
    mac = item["values"][0]
    history = load_history()
    name = history.get(mac, {}).get("name", "")
    
    if ROUTER_ENABLED and ROUTER_TYPE == "tp-link":
        if block_device_tplink(mac, name):
            scan_once()
        else:
            messagebox.showerror("Ошибка", f"Не удалось заблокировать устройство {mac}")
    else:
        messagebox.showinfo("Информация", "Режим роутера не включён или не поддерживается")

def unblock_selected():
    selection = device_tree.selection()
    if not selection:
        messagebox.showwarning("Внимание", "Выберите устройство в таблице")
        return
    item = device_tree.item(selection[0])
    mac = item["values"][0]
    
    if ROUTER_ENABLED and ROUTER_TYPE == "tp-link":
        if unblock_device_tplink(mac):
            scan_once()
        else:
            messagebox.showerror("Ошибка", f"Не удалось разблокировать устройство {mac}")
    else:
        messagebox.showinfo("Информация", "Режим роутера не включён или не поддерживается")

# === GUI ===
root = tk.Tk()
root.title("Монитор школьской сети")
root.geometry("900x700")
root.minsize(850, 650)

# === Кнопки
top_frame = tk.Frame(root)
top_frame.pack(pady=10, padx=10, fill='x')

def start_scan_in_thread():
    scan_btn.config(state='disabled')
    log_event("INFO", "scan_started", "🔍 Начинаю сканирование...")
    def run():
        scan_once()
        root.after(0, lambda: scan_btn.config(state='normal'))
    threading.Thread(target=run, daemon=True).start()

scan_btn = tk.Button(top_frame, text="Сканировать сейчас", command=start_scan_in_thread, width=20)
scan_btn.pack(side='left', padx=5)
start_btn = tk.Button(top_frame, text="Запустить фон", command=start_background, width=20)
start_btn.pack(side='left', padx=5)
stop_btn = tk.Button(top_frame, text="Остановить фон", command=stop_background, width=20, state='disabled')
stop_btn.pack(side='left', padx=5)
tk.Button(top_frame, text="Добавить всё в белый список", command=add_all_to_known, width=25).pack(side='left', padx=5)

# === Таблица устройств ===
columns = ("MAC", "IP", "Последнее появление", "Статус")
device_tree = ttk.Treeview(root, columns=columns, show="headings", height=15)
for col in columns:
    device_tree.heading(col, text=col)
    device_tree.column(col, width=180, anchor='w')
device_tree.pack(padx=10, pady=5, fill='both', expand=True)

# === Кнопки управления списком ===
list_btn_frame = tk.Frame(root)
list_btn_frame.pack(pady=5)
tk.Button(list_btn_frame, text="Добавить в белый список", command=add_selected_to_known).pack(side='left', padx=5)
tk.Button(list_btn_frame, text="Удалить из белого списка", command=remove_selected_from_known).pack(side='left', padx=5)
tk.Button(list_btn_frame, text="Заблокировать", command=block_selected, bg="#e74c3c", fg="white").pack(side='left', padx=5)
tk.Button(list_btn_frame, text="Разблокировать", command=unblock_selected, bg="#2ecc71", fg="white").pack(side='left', padx=5)

# === Журнал событий ===
tk.Label(root, text="Журнал событий").pack(anchor='w', padx=10)
log_listbox = Listbox(root, height=6)
log_listbox.pack(padx=10, pady=(0, 10), fill='both', expand=True)

# === Инициализация ===
def delayed_init():
    try:
        _, _, _, router_enabled, username, password, rtype, rip = load_configs()
        if router_enabled:
            log_event("INFO", "router_mode", f"✅ Режим роутера {rtype.upper()} активен ({rip})")
        else:
            log_event("INFO", "arp_mode", "🔄 Используется универсальный режим (arp -a)")

        history = load_history()
        known = load_known_devices()
        update_device_display(history, known)
        log_event("INFO", "startup_complete", "✅ Монитор школьской сети готов к работе.")
    except Exception as e:
        log_error_to_file()
        messagebox.showerror("Ошибка запуска", f"Программа завершилась с ошибкой:\n{str(e)}\n\nУбедитесь, что рядом есть config.ini")
        root.quit()

root.after(200, delayed_init)
root.mainloop()