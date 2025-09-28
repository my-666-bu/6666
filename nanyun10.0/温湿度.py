# 导入必要库
import matplotlib

matplotlib.use('Agg')
from flask import Flask
app = Flask(__name__)  # 创建Flask应用实例，这行代码必须在所有@app.route之前
import requests
import threading
import time as t
from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import os
from datetime import datetime
try:
    # 新大陆 SDK（优先使用）
    from nle_library.httpHelp.NetWorkBusiness import NetWorkBusiness
    NLE_SDK_AVAILABLE = True
except Exception as _e:
    NLE_SDK_AVAILABLE = False

# 核心配置
NLE_CONFIG = {
    'addr': 'api.nlecloud.com',
    'port': 80,
    'account': '15156882920',
    'pwd': '123456',
    'device_id': '1315431',
    'temp_key': 'wddhy',
    'humi_key': 'sddhy'
}
POINTS = 10  # 数据点数量（仅保留最近10个数据点）
MQTT_SIMULATE_INTERVAL = 3  # 模拟MQTT推送间隔（秒）

# 初始化数据存储
projects = [
    {"id": "proj001", "name": "默认项目", "desc": "系统默认项目",
     "create_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
]

devices = []

groups = [
    {"id": "group001", "name": "默认分组", "project_id": "proj001",
     "create_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
]

# 设备历史数据
device_history = {}

# 简易操作日志（仅内存，展示用）
activity_logs = []  # 每条：{'time': 'YYYY-MM-DD HH:MM:SS', 'user': 'xxx', 'action': '用户登录', 'detail': '...'}

def _now_ts_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def log_event(user: str, action: str, detail: str = ''):
    try:
        activity_logs.append({'time': _now_ts_str(), 'user': user or '-', 'action': action, 'detail': detail})
        # 仅保留最近100条
        if len(activity_logs) > 100:
            del activity_logs[:-100]
    except Exception:
        pass


@app.route('/api/logs', methods=['GET'])
def list_logs():
    user = session.get('user')
    if not user:
        return jsonify({'code': 401, 'msg': '未登录'})
    # 返回带索引（便于删除）
    items = [{'idx': i, **item} for i, item in enumerate(activity_logs)]
    return jsonify({'code': 200, 'data': items})


@app.route('/api/logs/<int:idx>', methods=['DELETE'])
def delete_log(idx: int):
    user = session.get('user')
    if not user:
        return jsonify({'code': 401, 'msg': '未登录'})
    if idx < 0 or idx >= len(activity_logs):
        return jsonify({'code': 404, 'msg': '日志不存在'})
    removed = activity_logs.pop(idx)
    log_event(user, '删除日志', f"{removed.get('action','')} {removed.get('time','')}")
    return jsonify({'code': 200, 'msg': '已删除'})


# 兼容 dashboard 表单提交方式：POST + ?_method=DELETE
@app.route('/api/logs/<int:idx>', methods=['POST'])
def delete_log_via_post(idx: int):
    if request.args.get('_method','').upper() == 'DELETE':
        return delete_log(idx)
    return jsonify({'code': 405, 'msg': '不支持的方法'})

# 初始化Flask和SocketIO
app = Flask(__name__)
app.config['SECRET_KEY'] = 'iot-platform-secret-key'
app.config['JSON_AS_ASCII'] = False  # 确保JSON输出为UTF-8，避免中文乱码
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*")

# 应用版本标识（用于排查是否启动到最新代码）
APP_VERSION = f"dashboard_login_v1@{datetime.now().strftime('%Y%m%d%H%M%S')}"

# 统一设置响应头中的字符集为UTF-8，避免HTML页面乱码
@app.after_request
def _set_utf8_charset(response):
    try:
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' in content_type and 'charset=' not in content_type:
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
    except Exception:
        pass
    return response


@app.route('/__version')
def __version():
    return jsonify({'version': APP_VERSION})


# 新大陆云平台API访问函数
def get_nlecloud_token():
    """获取新大陆云平台访问令牌"""
    url = f"http://{NLE_CONFIG['addr']}/Users/Login"
    data = {
        "Account": NLE_CONFIG['account'],
        "Password": NLE_CONFIG['pwd']
    }
    try:
        response = requests.post(url, json=data, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get('Status') == 0:
                return result.get('Result', {}).get('AccessToken')
        print("获取新大陆云平台Token失败:", response.text)
    except Exception as e:
        print("连接新大陆云平台失败:", str(e))
    return None


_nle_cloud_client = None
_nle_cloud_token_ready = False
_nle_cloud_last_login_ts = 0
_NLE_LOGIN_TTL_SEC = 55 * 60  # 55分钟后刷新


def _ensure_nle_cloud_client():
    """确保SDK客户端已登录并可用（账号+密码直登）。返回可用客户端或None。"""
    global _nle_cloud_client, _nle_cloud_token_ready, _nle_cloud_last_login_ts
    if not NLE_SDK_AVAILABLE:
        return None

    need_login = (
        _nle_cloud_client is None or
        not _nle_cloud_token_ready or
        (t.time() - _nle_cloud_last_login_ts) > _NLE_LOGIN_TTL_SEC
    )

    if need_login:
        try:
            cloud = NetWorkBusiness(NLE_CONFIG['addr'], NLE_CONFIG['port'])
            _nle_cloud_token_ready = False

            # Python 不允许在内嵌函数中直接赋值上层局部变量，这里改为闭包外赋值
            def _make_login_cb():
                def cb(dat):
                    try:
                        token = dat['ResultObj']['AccessToken']
                        cloud.setAccessToken(token)
                        # 使用全局标记
                        global _nle_cloud_token_ready, _nle_cloud_last_login_ts, _nle_cloud_client
                        _nle_cloud_token_ready = True
                        _nle_cloud_last_login_ts = t.time()
                        _nle_cloud_client = cloud
                    except Exception as e:
                        print('NLE SDK 设置token失败:', e)
                return cb

            cloud.signIn(NLE_CONFIG['account'], NLE_CONFIG['pwd'], _make_login_cb())
            # 等待回调设置令牌，最多等待5秒
            start_wait = t.time()
            while not _nle_cloud_token_ready and (t.time() - start_wait) < 5:
                t.sleep(0.05)
        except Exception as e:
            print('NLE SDK 登录异常:', e)

    return _nle_cloud_client if _nle_cloud_token_ready else None
# 通用工具函數
def generate_nle_device_id(nle_device_id: str, device_type: str, nle_sensor_key: str) -> str:
    """根據設備類型/標識生成穩定唯一的NLE設備ID。"""
    if not nle_device_id:
        return ''
    if device_type == 'temperature':
        suffix = 'temp'
    elif device_type == 'humidity':
        suffix = 'humi'
    else:
        raw_key = nle_sensor_key or ''
        safe_key = ''.join(c if (c.isalnum() or c in ('-', '_')) else '_' for c in raw_key)
        safe_key = (safe_key.strip('_') or 'key')[:20]
        suffix = safe_key
    return f"nle_{nle_device_id}_{suffix}"


def matches_device(dev: dict, keyword_lower: str, device_type: str = '') -> bool:
    """是否匹配設備（名稱/ID/標識、類型）。keyword須為lower。"""
    if device_type and dev.get('device_type') != device_type:
        return False
    name_l = (dev.get('name') or '').lower()
    id_l = (dev.get('id') or '').lower()
    key_l = (dev.get('nle_sensor_key') or '').lower()
    ctrl_key_l = (dev.get('controller_key') or '').lower()
    return (keyword_lower in name_l) or (keyword_lower in id_l) or (keyword_lower in key_l) or (keyword_lower in ctrl_key_l)


def get_nlecloud_device_data(device_id, sensor_key):
    """获取新大陆云平台设备数据。优先使用SDK，失败时回退HTTP。"""
    # 1) SDK 路径
    cloud = _ensure_nle_cloud_client()
    if cloud is not None:
        try:
            res = cloud.getSensor(device_id, sensor_key)
            # 期望结构：{'ResultObj': {'Value': <number>}, ...}
            if isinstance(res, dict):
                result_obj = res.get('ResultObj') or res.get('Result') or {}
                value = result_obj.get('Value')
                if value is not None:
                    return value
        except Exception as e:
            print(f'NLE SDK 获取设备{device_id} 传感{sensor_key}失败:', e)

    # 2) HTTP 回退
    token = get_nlecloud_token()
    if not token:
        return None
    url = f"http://{NLE_CONFIG['addr']}/Devices/{device_id}/Sensors/{sensor_key}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get('Status') == 0:
                return result.get('Result', {}).get('Value')
        print(f"HTTP获取设备{device_id}数据失败:", response.text)
    except Exception as e:
        print(f"HTTP获取设备{device_id}数据异常:", str(e))
    return None


# MQTT模拟数据推送
def mqtt_simulator():
    """模拟MQTT订阅并推送数据"""
    while True:
        # 为每个设备生成数据
        for dev in devices:
            # 控制器不参与数值推送与历史曲线
            if dev.get('device_type') == 'controller':
                continue
            # 检查是否为新大陆设备
            if dev.get('is_nle_device') and dev.get('nle_device_id'):
                # 根据设备类型从新大陆云平台获取对应的真实数据
                if dev.get('device_type') == 'temperature':
                    temp_key = dev.get('nle_sensor_key') or NLE_CONFIG['temp_key']
                    temp = get_nlecloud_device_data(dev['nle_device_id'], temp_key)
                    if temp is not None:
                        new_temp = float(temp)
                    else:
                        new_temp = dev['temp']
                    # 湿度值对于温度设备不更新
                    new_humidity = dev['humi']
                elif dev.get('device_type') == 'humidity':
                    humi_key = dev.get('nle_sensor_key') or NLE_CONFIG['humi_key']
                    humi = get_nlecloud_device_data(dev['nle_device_id'], humi_key)
                    if humi is not None:
                        new_humidity = float(humi)
                    else:
                        new_humidity = dev['humi']
                    # 温度值对于湿度设备不更新
                    new_temp = dev['temp']
                else:
                    # 通用设备：仅按自定义Key获取单值，放入temp通道以便前端单折线展示
                    generic_key = dev.get('nle_sensor_key') or NLE_CONFIG['temp_key']
                    val = get_nlecloud_device_data(dev['nle_device_id'], generic_key)
                    if val is not None:
                        new_temp = float(val)
                    else:
                        new_temp = dev['temp']
                    new_humidity = dev['humi']
            else:
                # 普通设备保持原有数据（不做随机/模拟变动）
                new_temp = dev['temp']
                new_humidity = dev['humi']

            # 更新设备当前值
            dev['temp'] = new_temp
            dev['humi'] = new_humidity

            # 更新历史数据
            current_time = datetime.now().strftime('%H:%M:%S')
            history = device_history[dev['id']]

            # 移除最旧数据，添加新数据
            history['time'] = history['time'][1:] + [current_time]
            history['temp'] = history['temp'][1:] + [new_temp]
            history['humi'] = history['humi'][1:] + [new_humidity]

            # 通过SocketIO推送实时数据到前端
            socketio.emit('sensor_data', {
                'device_id': dev['id'],
                'name': dev['name'],
                'temp': new_temp,
                'humi': new_humidity,
                'time': current_time,
                'history': history
            })

        # 等待下一次推送
        t.sleep(MQTT_SIMULATE_INTERVAL)


def _preload_default_nle_devices():
    """预置添加两台NLE设备：温度与湿度，使用同一NLE设备ID。"""
    nle_id = NLE_CONFIG.get('device_id')
    if not nle_id:
        return

    # 设备唯一ID，区分温度/湿度
    temp_id = f"nle_{nle_id}_temp"
    humi_id = f"nle_{nle_id}_humi"

    existing_ids = set(d['id'] for d in devices)

    # 确保存在默认分组
    project_id = projects[0]['id'] if projects else 'proj001'
    group_id = groups[0]['id'] if groups else 'group001'

    # 初始化当前值（尽量从云端取）
    now_time = datetime.now().strftime('%H:%M:%S')

    if temp_id not in existing_ids:
        temp_val = 0.0
        fetched = get_nlecloud_device_data(nle_id, NLE_CONFIG['temp_key'])
        if fetched is not None:
            try:
                temp_val = float(fetched)
            except Exception:
                pass
        device = {
            'id': temp_id,
            'name': '温度传感器',
            'project_id': project_id,
            'group_id': group_id,
            'temp': temp_val,
            'humi': 0.0,
            'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'is_nle_device': True,
            'nle_device_id': str(nle_id),
            'device_type': 'temperature',
            'nle_sensor_key': NLE_CONFIG.get('temp_key')
        }
        devices.append(device)
        device_history[temp_id] = {
            'time': [now_time] * POINTS,
            'temp': [temp_val] * POINTS,
            'humi': [0.0] * POINTS
        }

    if humi_id not in existing_ids:
        humi_val = 0.0
        fetched = get_nlecloud_device_data(nle_id, NLE_CONFIG['humi_key'])
        if fetched is not None:
            try:
                humi_val = float(fetched)
            except Exception:
                pass
        device = {
            'id': humi_id,
            'name': '湿度传感器',
            'project_id': project_id,
            'group_id': group_id,
            'temp': 0.0,
            'humi': humi_val,
            'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'is_nle_device': True,
            'nle_device_id': str(nle_id),
            'device_type': 'humidity',
            'nle_sensor_key': NLE_CONFIG.get('humi_key')
        }
        devices.append(device)
        device_history[humi_id] = {
            'time': [now_time] * POINTS,
            'temp': [0.0] * POINTS,
            'humi': [humi_val] * POINTS
        }


# 路由定义
@app.route('/')
def index():
    # 若已登录，跳转仪表板；否则到登录
    if session.get('user'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # 任意账号密码皆可登录
        username = (request.form.get('username') or '').strip() or 'guest'
        _ = request.form.get('password')  # 不校验
        session['user'] = username
        log_event(username, '用户登录', '登录成功')
        return redirect(url_for('dashboard'))
    # GET 显示登录页
    return render_template('login.html')


@app.route('/logout')
def logout():
    user = session.get('user') or ''
    session.pop('user', None)
    log_event(user, '用户登出', '手动退出')
    return redirect(url_for('login'))


@app.route('/dashboard')
def dashboard():
    user = session.get('user')
    if not user:
        return redirect(url_for('login'))
    # 最近操作日志（倒序显示，仅取近10条），附带在全量中的索引以支持删除
    start = max(0, len(activity_logs) - 10)
    recent_slice = [(idx, activity_logs[idx]) for idx in range(start, len(activity_logs))]
    recent = []
    for idx, item in reversed(recent_slice):
        row = dict(item)
        row['idx'] = idx
        recent.append(row)
    now_str = _now_ts_str()
    return render_template('dashboard.html', user=user, now_str=now_str, logs=recent,
                           device_total=len(devices),
                           online_users=1)


@app.route('/devices')
def devices_page():
    user = session.get('user')
    if not user:
        return redirect(url_for('login'))
    # 全部設備清單
    return render_template('devices.html', devices=devices)


@app.route('/device/<device_id>', methods=['GET'])
def device_detail_page(device_id):
    user = session.get('user')
    if not user:
        return redirect(url_for('login'))
    dev = next((d for d in devices if d['id'] == device_id), None)
    if not dev:
        return redirect(url_for('devices_page'))
    # 同项目与分组的设备，用于列表
    sibling_devices = [d for d in devices if d['project_id'] == dev['project_id']]
    return render_template('device_detail.html', device=dev, devices=sibling_devices,
                           projects=projects, groups=groups)


@app.route('/sensor/<device_id>')
def sensor_detail(device_id):
    device = next((d for d in devices if d['id'] == device_id), None)
    if not device:
        return render_template('tuxiang.html')
    return render_template('sensor_detail.html', device=device)


@app.route('/search')
def search_page():
    """小页面展示搜索结果（简体中文）。使用与 /api/devices/search 相同的匹配规则。"""
    keyword = (request.args.get('keyword') or '').strip()
    results = []
    if keyword:
        kw_lower = keyword.lower()
        for d in devices:
            name_l = (d.get('name') or '').lower()
            id_l = (d.get('id') or '').lower()
            key_l = (d.get('nle_sensor_key') or '').lower()
            if kw_lower in name_l or kw_lower in id_l or kw_lower in key_l:
                results.append(d)
    return render_template('search_results.html', keyword=keyword, results=results)


@app.route('/controller/<device_id>')
def controller_detail(device_id):
    device = next((d for d in devices if d['id'] == device_id and d.get('device_type') == 'controller'), None)
    if not device:
        return render_template('tuxiang.html')
    return render_template('controller_detail.html', device=device)


# API接口
# 项目相关
@app.route('/api/projects', methods=['GET'])
def get_projects():
    return jsonify({'code': 200, 'data': projects})


@app.route('/api/projects', methods=['POST'])
def add_project():
    data = request.json
    if not data.get('name'):
        return jsonify({'code': 400, 'msg': '项目名称不能为空'})

    # 创建项目
    project_id = f"proj{int(t.time())}"
    new_project = {
        'id': project_id,
        'name': data['name'],
        'desc': data.get('desc', '无描述'),
        'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    projects.append(new_project)

    # 自动创建默认分组
    group_id = f"group{int(t.time())}"
    new_group = {
        'id': group_id,
        'name': f"{data['name']}默认分组",
        'project_id': project_id,
        'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    groups.append(new_group)

    return jsonify({
        'code': 200,
        'msg': '项目创建成功',
        'data': {'project': new_project, 'default_group': new_group}
    })


@app.route('/api/projects/<project_id>', methods=['DELETE'])
def delete_project(project_id):
    global projects, groups, devices
    # 删除项目
    project = next((p for p in projects if p['id'] == project_id), None)
    if not project:
        return jsonify({'code': 404, 'msg': '项目不存在'})
    projects.remove(project)

    # 删除项目下的分组
    project_groups = [g for g in groups if g['project_id'] == project_id]
    for g in project_groups:
        groups.remove(g)

    # 删除项目下的设备
    project_devices = [d for d in devices if d['project_id'] == project_id]
    for d in project_devices:
        devices.remove(d)
        if d['id'] in device_history:
            del device_history[d['id']]

    return jsonify({'code': 200, 'msg': '项目删除成功'})


# 设备相关
@app.route('/api/devices', methods=['GET'])
def get_devices():
    project_id = request.args.get('project_id')
    device_type = request.args.get('device_type')

    try:
        print(f"[API] /api/devices GET project_id={project_id} device_type={device_type}")
    except Exception:
        pass

    filtered_devices = devices

    if project_id:
        filtered_devices = [d for d in filtered_devices if d['project_id'] == project_id]

    if device_type:
        filtered_devices = [d for d in filtered_devices if d['device_type'] == device_type]

    return jsonify({'code': 200, 'data': filtered_devices})


@app.route('/api/devices', methods=['POST'])
def add_device():
    data = request.json
    if not all(k in data for k in ['name', 'project_id']):
        return jsonify({'code': 400, 'msg': '设备名称和所属项目不能为空'})

    # 检查是否为新大陆设备
    is_nle_device = data.get('is_nle_device', False)
    nle_device_id = data.get('nle_device_id', '')
    nle_sensor_key = data.get('nle_sensor_key', '')
    device_type = data.get('device_type', '')  # 前端可不传，由后端推断

    # 查找项目
    project = next((p for p in projects if p['id'] == data['project_id']), None)
    if not project:
        return jsonify({'code': 404, 'msg': '项目不存在'})

    # 查找项目的默认分组
    project_groups = [g for g in groups if g['project_id'] == data['project_id']]
    if not project_groups:
        return jsonify({'code': 404, 'msg': '项目分组不存在'})
    group_id = project_groups[0]['id']  # 使用第一个分组

    # 如果传入了nle_sensor_key但未提供nle_device_id，则默认使用全局NLE设备ID并视为NLE设备
    if (not nle_device_id) and nle_sensor_key:
        nle_device_id = str(NLE_CONFIG.get('device_id') or '')
        is_nle_device = True if nle_device_id else is_nle_device

    # 推断设备类型：优先匹配默认温湿度Key，否则为generic
    if not device_type:
        if nle_sensor_key and nle_sensor_key == NLE_CONFIG['temp_key']:
            device_type = 'temperature'
        elif nle_sensor_key and nle_sensor_key == NLE_CONFIG['humi_key']:
            device_type = 'humidity'
        else:
            device_type = 'generic'

    # 创建设备
    if is_nle_device and nle_device_id:
        device_id = generate_nle_device_id(nle_device_id, device_type, nle_sensor_key)
    else:
        # 根据设备类型生成不同的ID前缀
        prefix = "temp" if device_type == "temperature" else ("humi" if device_type == "humidity" else "gen")
        device_id = f"{prefix}{int(t.time())}"

    # 设置初始值（不会进行随机或模拟，仅作占位）
    temp_value = 0.0
    humi_value = 0.0

    # 如果是新大陆设备，尝试获取实时数据
    if is_nle_device and nle_device_id:
        if device_type == 'temperature':
            key = nle_sensor_key or NLE_CONFIG['temp_key']
            temp = get_nlecloud_device_data(nle_device_id, key)
            if temp is not None:
                temp_value = float(temp)
        elif device_type == 'humidity':
            key = nle_sensor_key or NLE_CONFIG['humi_key']
            humi = get_nlecloud_device_data(nle_device_id, key)
            if humi is not None:
                humi_value = float(humi)
        else:
            key = nle_sensor_key or NLE_CONFIG['temp_key']
            val = get_nlecloud_device_data(nle_device_id, key)
            if val is not None:
                temp_value = float(val)

    new_device = {
        'id': device_id,
        'name': data['name'],
        'project_id': data['project_id'],
        'group_id': group_id,
        'temp': temp_value,
        'humi': humi_value,
        'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'is_nle_device': is_nle_device,
        'nle_device_id': nle_device_id if is_nle_device else '',
        'device_type': device_type,
        'nle_sensor_key': nle_sensor_key if is_nle_device else ''
    }
    devices.append(new_device)
    log_event(session.get('user') or 'system', '添加设备', new_device.get('name') or new_device.get('id'))

    # 初始化历史数据
    init_time = [datetime.now().strftime('%H:%M:%S')] * POINTS
    device_history[device_id] = {
        'time': init_time,
        'temp': [temp_value] * POINTS,
        'humi': [humi_value] * POINTS
    }

    return jsonify({'code': 200, 'msg': '设备添加成功', 'data': new_device})


@app.route('/device/<device_id>/add_sensor', methods=['POST'])
def device_add_sensor(device_id):
    user = session.get('user')
    if not user:
        return redirect(url_for('login'))
    # 从表单一次性提交
    name = (request.form.get('name') or '').strip() or '新传感器'
    is_nle_device = bool(request.form.get('is_nle_device'))
    nle_device_id = (request.form.get('nle_device_id') or '').strip()
    nle_sensor_key = (request.form.get('nle_sensor_key') or '').strip()

    dev = next((d for d in devices if d['id'] == device_id), None)
    if not dev:
        return redirect(url_for('devices_page'))

    # 不再从表单传入设备类型，交由后端在 add_device 中根据 key 自动推断
    req_json = {
        'name': name,
        'project_id': dev['project_id'],
        'is_nle_device': is_nle_device,
        'nle_device_id': nle_device_id,
        'nle_sensor_key': nle_sensor_key
    }
    # 直接调用内部方法逻辑
    with app.test_request_context(json=req_json):
        resp = add_device()
        try:
            data = resp.get_json().get('data')
            log_event(user, '添加传感器', data.get('name'))
        except Exception:
            pass
    return redirect(url_for('device_detail_page', device_id=device_id))


@app.route('/device/<device_id>/add_controller', methods=['POST'])
def device_add_controller(device_id):
    user = session.get('user')
    if not user:
        return redirect(url_for('login'))
    name = (request.form.get('name') or '').strip() or '新控制器'
    controller_key = (request.form.get('controller_key') or '').strip()
    control_mode = (request.form.get('control_mode') or '').strip().lower()
    control_http_url = (request.form.get('control_http_url') or '').strip()
    nle_device_id = (request.form.get('nle_device_id') or '').strip()
    nle_control_key = (request.form.get('nle_control_key') or '').strip()

    dev = next((d for d in devices if d['id'] == device_id), None)
    if not dev:
        return redirect(url_for('devices_page'))

    payload = {
        'name': name,
        'project_id': dev['project_id'],
        'controller_key': controller_key,
        'control_mode': control_mode,
        'control_http_url': control_http_url,
        'nle_device_id': nle_device_id,
        'nle_control_key': nle_control_key
    }

    with app.test_request_context(json=payload):
        resp = add_controller()
        try:
            data = resp.get_json().get('data')
            log_event(user, '添加控制器', data.get('name'))
        except Exception:
            pass
    return redirect(url_for('device_detail_page', device_id=device_id))


# 控制器相关 API
@app.route('/api/controllers', methods=['POST'])
def add_controller():
    data = request.json or {}
    name = data.get('name')
    project_id = data.get('project_id')
    controller_key = (data.get('controller_key') or '').strip()
    control_http_url = (data.get('control_http_url') or '').strip()
    control_mode = (data.get('control_mode') or '').strip().lower()  # http | nle
    nle_ctrl_device_id = (data.get('nle_device_id') or '').strip()
    nle_ctrl_key = (data.get('nle_control_key') or '').strip()
    if not name or not project_id:
        return jsonify({'code': 400, 'msg': '控制器名称和所属项目不能为空'})

    # 查找项目与其分组
    project = next((p for p in projects if p['id'] == project_id), None)
    if not project:
        return jsonify({'code': 404, 'msg': '项目不存在'})
    project_groups = [g for g in groups if g['project_id'] == project_id]
    if not project_groups:
        return jsonify({'code': 404, 'msg': '项目分组不存在'})
    group_id = project_groups[0]['id']

    # 生成唯一ID，带上标识
    safe_key = ''.join(c if (c.isalnum() or c in ('-', '_')) else '_' for c in controller_key)[:20] or 'ctrl'
    device_id = f"ctrl{int(t.time())}_{safe_key}"
    # 推断控制模式与控制key（如未提供，默认走NLE并使用controller_key）
    inferred_mode = control_mode if control_mode in ('http', 'nle') else ('http' if control_http_url else 'nle')
    if inferred_mode == 'nle' and not nle_ctrl_key:
        nle_ctrl_key = controller_key

    new_controller = {
        'id': device_id,
        'name': name,
        'project_id': project_id,
        'group_id': group_id,
        'temp': 0.0,
        'humi': 0.0,
        'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'is_nle_device': False,
        'nle_device_id': '',
        'device_type': 'controller',
        'nle_sensor_key': '',
        'controller_key': controller_key,
        'control_http_url': control_http_url,
        'control_mode': inferred_mode,
        'nle_ctrl_device_id': nle_ctrl_device_id,
        'nle_ctrl_key': nle_ctrl_key,
        # 单执行器状态：True=开，False=关
        'state': False
    }
    devices.append(new_controller)
    log_event(session.get('user') or 'system', '添加控制器', new_controller.get('name') or new_controller.get('id'))
    return jsonify({'code': 200, 'msg': '控制器添加成功', 'data': new_controller})


@app.route('/api/controllers/<device_id>/toggle', methods=['POST'])
def toggle_controller(device_id):
    data = request.json or {}
    # 仅单一执行器开关
    state = bool(data.get('state'))
    try:
        print(f"[API] TOGGLE controller id={device_id} -> state={state}")
    except Exception:
        pass
    dev = next((d for d in devices if d['id'] == device_id and d.get('device_type') == 'controller'), None)
    if not dev:
        return jsonify({'code': 404, 'msg': '控制器不存在'})
    # 兼容旧数据结构
    if 'state' not in dev and 'controls' in dev:
        # 若旧结构存在，取任一（优先fan）转为单开关
        old = dev.get('controls') or {}
        dev['state'] = bool(old.get('fan') or old.get('bulb'))
        dev.pop('controls', None)
    # 下发：HTTP 或 NLE API
    ctl_mode = (dev.get('control_mode') or '').lower()
    if ctl_mode == 'http':
        url = (dev.get('control_http_url') or '').strip()
        if url:
            payload = {'id': device_id, 'key': dev.get('controller_key', ''), 'state': 1 if state else 0}
            try:
                resp = requests.post(url, json=payload, timeout=5)
                if not (200 <= resp.status_code < 300):
                    return jsonify({'code': 502, 'msg': f'下发HTTP失败: {resp.status_code} {resp.text[:200]}'})
            except Exception as e:
                return jsonify({'code': 502, 'msg': f'下发HTTP异常: {e}'})
    elif ctl_mode == 'nle':
        try:
            cloud = _ensure_nle_cloud_client() or NetWorkBusiness(NLE_CONFIG['addr'], NLE_CONFIG['port'])
            # 若是 NetWorkBusiness，需登录并设置token
            if not _nle_cloud_token_ready:
                def _cb(dat):
                    try:
                        token = dat['ResultObj']['AccessToken']
                        cloud.setAccessToken(token)
                    except Exception as _:
                        pass
                cloud.signIn(NLE_CONFIG['account'], NLE_CONFIG['pwd'], _cb)
                t.sleep(0.2)
            r = cloud.control(dev.get('nle_ctrl_device_id') or NLE_CONFIG['device_id'], dev.get('nle_ctrl_key') or 'm_multi_red', 1 if state else 0)
            # r 是请求返回内容，这里不做深入解析
        except Exception as e:
            return jsonify({'code': 502, 'msg': f'NLE控制失败: {e}'})

    dev['state'] = state
    return jsonify({'code': 200, 'msg': '状态已更新', 'data': {
        'id': dev['id'],
        'state': dev['state'],
        'controller_key': dev.get('controller_key', '')
    }})


@app.route('/api/devices/<device_id>', methods=['DELETE'])
def delete_device(device_id):
    global devices
    device = next((d for d in devices if d['id'] == device_id), None)
    if not device:
        return jsonify({'code': 404, 'msg': '设备不存在'})

    devices.remove(device)
    log_event(session.get('user') or 'system', '删除设备', device.get('name') or device_id)
    if device_id in device_history:
        del device_history[device_id]

    return jsonify({'code': 200, 'msg': '设备删除成功'})


@app.route('/api/devices/search', methods=['GET'])
def search_devices():
    keyword = request.args.get('keyword', '').lower()
    device_type = request.args.get('device_type', '')

    if not keyword:
        return jsonify({'code': 400, 'msg': '搜索关键词不能为空'})

    try:
        print(f"[API] /api/devices/search keyword='{keyword}' device_type='{device_type}'")
    except Exception:
        pass

    result = [d for d in devices if matches_device(d, keyword, device_type)]
    return jsonify({'code': 200, 'data': result})


@app.route('/api/devices/<device_id>/history', methods=['GET'])
def get_device_history(device_id):
    history = device_history.get(device_id)
    if not history:
        return jsonify({'code': 404, 'msg': '设备不存在'})
    return jsonify({'code': 200, 'data': history})


# 分组相关
@app.route('/api/groups', methods=['GET'])
def get_groups():
    project_id = request.args.get('project_id')
    if project_id:
        filtered = [g for g in groups if g['project_id'] == project_id]
        return jsonify({'code': 200, 'data': filtered})
    return jsonify({'code': 200, 'data': groups})


@app.route('/api/groups/<group_id>/devices', methods=['GET'])
def get_group_devices(group_id):
    group_devices = [d for d in devices if d['group_id'] == group_id]
    return jsonify({'code': 200, 'data': group_devices})


# SocketIO事件处理
@socketio.on('connect')
def handle_connect():
    print('客户端已连接')
    emit('connection_ack', {'message': '连接成功'})


@socketio.on('disconnect')
def handle_disconnect():
    print('客户端已断开连接')


@socketio.on('subscribe_device')
def handle_subscribe(device_id):
    """客户端订阅特定设备的数据推送"""
    print(f'客户端订阅设备: {device_id}')
    # 立即推送一次当前数据
    device = next((d for d in devices if d['id'] == device_id), None)
    if device and device_id in device_history:
        emit('sensor_data', {
            'device_id': device_id,
            'name': device['name'],
            'temp': device['temp'],
            'humi': device['humi'],
            'time': datetime.now().strftime('%H:%M:%S'),
            'history': device_history[device_id]
        })


# 启动服务
if __name__ == '__main__':
    # 创建必要文件夹
    for folder in ['templates', 'static']:
        if not os.path.exists(folder):
            os.makedirs(folder)

    # 预置默认NLE设备
    _preload_default_nle_devices()

    # 启动MQTT模拟线程
    mqtt_thread = threading.Thread(target=mqtt_simulator, daemon=True)
    mqtt_thread.start()

    # 若无初始日志，添加一条系统启动记录
    if not activity_logs:
        log_event('system', '系统启动', '服务已启动')

    # 启动Flask-SocketIO服务（改用5001以避开旧进程）
    print(f"服务启动({APP_VERSION})，访问 http://localhost:5001")
    socketio.run(app, host='0.0.0.0', port=5001, debug=True)