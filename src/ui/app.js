/**
 * Dell Fan Sense - Apple HIG Frontend Application
 * Interacts with Python APIBridge via window.pywebview.api
 */

// Application State
const state = {
  connected: false,
  latency_ms: 0,
  mode: 'auto', // 默认 Dell 原厂安全托管 ('auto', 'dynamic', 'manual', 'preset')
  current_target_speed: null,
  max_cpu_temp: null,
  inlet_temp: null,
  cpu_temps: [],
  fans: [],
  all_sensors: [],
  safety_triggered: false,
  demo_mode: false,
  autostart_active: false,
  activeTab: 'dashboard',
  dashboardViewMode: 'probe', // 'probe' (探针矩阵) or 'detail' (单机详情)
  uiRefreshSec: 1,             // 默认 1 秒界面 UI 刷新平滑渲染周期 (与后端采样独立解耦)
  autoRefreshSec: 3,           // 底层采集轮询间隔或模式
  sensorCategory: 'all',
  sensorSearchTerm: '',
  selectedServerIds: new Set(),
  probeFilter: 'all',
  probeLayoutStyle: 'compact_grid',
  clusterViewMode: 'grid',       // 'grid' or 'row'
  conceptView: 'overview',       // 'overview' (总览), 'nodes' (硬件节点), 'servers' (系统服务器)
  tabGlobalSpeed: 25,
  config: {},
  servers: [
    { id: 'srv_primary', name: 'Dell PowerEdge #1', ip: '192.168.1.1', user: 'root', model: 'PowerEdge R730', mode: 'dynamic', manual_speed: 25, enabled: true },
    { id: 'srv_dd193f', name: 'Dell PowerEdge #2', ip: '192.168.1.102', user: 'root', model: 'PowerEdge R740xd', mode: 'dynamic', manual_speed: 25, enabled: true }
  ],                   // 硬件节点列表 (IPMI)
  system_servers: [
    { id: 'srv_pve_bound', name: 'PVE 虚拟化母机', host: '192.168.1.100', node_id: 'srv_primary', os_name: 'Proxmox VE 8.1', enabled: true },
    { id: 'srv_standalone_ubuntu', name: '独立应用服务器', host: '192.168.1.150', node_id: '', os_name: 'Ubuntu 22.04 LTS', enabled: true }
  ],            // 系统服务器列表 (SSH)
  activeServer: null,
  cluster_telemetry: [],
  subsystems: {},                // 兼容保留
  subsystem_drawer_open: {},     // node_id -> boolean (总览中折叠/展开绑定的服务器)
  alert_history: [],
  alert_config: {},
  subsystem_poll_sec: 2,
  editing_server_subsystems: [], // Temporary in-memory list for modal editor
  ops_selected_server_ids: new Set(), // 运维平台已选服务器 ID
  ops_filter_keyword: '',             // 运维平台搜索过滤词
  ops_last_results: [],               // 运维平台最近一次执行结果
  curve_nodes: [
    { temp: 45, speed: 15, name: "静音基准" },
    { temp: 55, speed: 22, name: "日常轻载" },
    { temp: 65, speed: 32, name: "中载巡航" },
    { temp: 72, speed: 45, name: "温升加速" },
    { temp: 78, speed: 70, name: "重载强冷" },
    { temp: 82, speed: 100, is_safety: true, name: "BMC熔断保护" }
  ]
};

let pollIntervalTimer = null;
let editingServerId = null;

// Preset catalog definition
const PRESETS = [
  {
    key: 'silent',
    title: '静音模式 (Silent)',
    desc: '全机箱风扇统一 15%，极致静音，适合低负荷办公与居家静音场景。',
    speeds: [15, 15, 15, 15, 15, 15],
    iconSvg: `<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>`
  },
  {
    key: 'one_node_l',
    title: '单节点左侧负载 (L-Node)',
    desc: '左侧节点强风散热 [25, 40, 30, 25, 20, 20]，针对单CPU或左侧PCIE卡高载。',
    speeds: [25, 40, 30, 25, 20, 20],
    iconSvg: `<rect x="3" y="3" width="8" height="18" rx="2"/><rect x="13" y="3" width="8" height="18" rx="2" opacity="0.3"/>`
  },
  {
    key: 'one_node_r',
    title: '单节点右侧负载 (R-Node)',
    desc: '右侧节点强风散热 [20, 20, 25, 30, 40, 25]，针对右侧单CPU或GPU高载。',
    speeds: [20, 20, 25, 30, 40, 25],
    iconSvg: `<rect x="3" y="3" width="8" height="18" rx="2" opacity="0.3"/><rect x="13" y="3" width="8" height="18" rx="2"/>`
  },
  {
    key: 'two_node_eco',
    title: '双节点节能平衡 (ECO)',
    desc: '双路CPU均衡轻载 [15, 23, 20, 20, 23, 15]，噪音与风量兼顾。',
    speeds: [15, 23, 20, 20, 23, 15],
    iconSvg: `<path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>`
  },
  {
    key: 'two_node_perf',
    title: '双节点高性能 (125W+)',
    desc: '双路CPU高负荷 [25, 40, 30, 30, 40, 25]，适合渲染、编译与严苛计算。',
    speeds: [25, 40, 30, 30, 40, 25],
    iconSvg: `<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>`
  },
  {
    key: 'turbo',
    title: '满载强效散热 (Turbo)',
    desc: '全风扇 80%，强效快速带走机箱积热，应对突发严苛高负载。',
    speeds: [80, 80, 80, 80, 80, 80],
    iconSvg: `<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>`
  }
];

// Helper: Safely access pywebview backend API
async function callApi(method, ...args) {
  if (window.pywebview && window.pywebview.api && typeof window.pywebview.api[method] === 'function') {
    return await window.pywebview.api[method](...args);
  } else {
    // If running in browser or API disconnected, provide full realistic client-side simulation!
    return handleClientSideFallback(method, ...args);
  }
}

// Client-side pure simulation fallback: ensures UI ALWAYS works and has values even if API is pending
function handleClientSideFallback(method, ...args) {
  if (method === 'toggle_demo_mode') {
    const enable = !!args[0];
    state.demo_mode = enable;
    state.connected = enable;
    if (enable) {
      // 演示模式构建涵盖 Dell、浪潮 Inspur、华为 Huawei 多品牌与序列号资产的逼真演示数据
      state.demo_servers = [
        { 
          id: 'srv_primary', 
          name: 'Dell 核心计算节点', 
          ip: '192.168.1.10', 
          user: 'root', 
          password: '', 
          brand: 'dell', 
          model: 'PowerEdge R730xd', 
          serial: '7X89B22', 
          mode: 'dynamic', 
          manual_speed: 25, 
          preset_key: 'silent', 
          enabled: true 
        },
        { 
          id: 'srv_inspur', 
          name: '浪潮 分布式存储节点', 
          ip: '192.168.1.20', 
          user: 'admin', 
          password: '', 
          brand: 'inspur', 
          model: 'NF5280M5', 
          serial: '2198038123', 
          mode: 'preset', 
          manual_speed: 20, 
          preset_key: 'silent', 
          enabled: true 
        },
        { 
          id: 'srv_huawei', 
          name: '华为 异构推理服务器', 
          ip: '192.168.1.30', 
          user: 'Administrator', 
          password: '', 
          brand: 'huawei', 
          model: 'FusionServer 2288H V5', 
          serial: '2102311WUT10J3000', 
          mode: 'auto', 
          manual_speed: 30, 
          preset_key: 'two_node_eco', 
          enabled: true 
        }
      ];
      state.demo_system_servers = [
        {
          id: 'srv_pve_bound',
          name: 'PVE 虚拟化母机 (已绑定 Dell 节点)',
          host: '192.168.1.100',
          port: 22,
          username: 'root',
          password: '',
          node_id: 'srv_primary',
          enabled: true,
          connected: true,
          cpu_pct: 26.5,
          cpu_cores: 16,
          mem_pct: 42.0,
          mem_used_gb: 26.8,
          mem_total_gb: 64.0,
          swap_pct: 4.2,
          swap_used_gb: 0.3,
          swap_total_gb: 8.0,
          disk_pct: 35.0,
          disk_used_gb: 358.4,
          disk_total_gb: 1024.0,
          uptime_sec: 86400 * 24,
          hostname: 'pve-master-01'
        },
        {
          id: 'srv_ceph_bound',
          name: 'Ceph OSD 存储守护 (已绑定浪潮节点)',
          host: '192.168.1.120',
          port: 22,
          username: 'root',
          password: '',
          node_id: 'srv_inspur',
          enabled: true,
          connected: true,
          cpu_pct: 18.2,
          cpu_cores: 32,
          mem_pct: 58.0,
          mem_used_gb: 74.2,
          mem_total_gb: 128.0,
          swap_pct: 0.0,
          swap_used_gb: 0.0,
          swap_total_gb: 16.0,
          disk_pct: 62.0,
          disk_used_gb: 12697.6,
          disk_total_gb: 20480.0,
          uptime_sec: 86400 * 45,
          hostname: 'inspur-ceph-osd01'
        },
        {
          id: 'srv_standalone_ubuntu',
          name: '独立应用服务器',
          host: '192.168.1.150',
          port: 22,
          username: 'ubuntu',
          password: '',
          node_id: '',
          enabled: true,
          connected: true,
          cpu_pct: 14.2,
          cpu_cores: 8,
          mem_pct: 31.5,
          mem_used_gb: 10.0,
          mem_total_gb: 32.0,
          swap_pct: 0.0,
          swap_used_gb: 0.0,
          swap_total_gb: 4.0,
          disk_pct: 28.0,
          disk_used_gb: 143.3,
          disk_total_gb: 512.0,
          uptime_sec: 86400 * 12,
          hostname: 'ubuntu-app-prod'
        }
      ];
      state.servers = state.demo_servers;
      state.system_servers = state.demo_system_servers;

      state.max_cpu_temp = 54.5;
      state.inlet_temp = 21.0;
      state.current_target_speed = 25;
      state.latency_ms = 11;
      state.cpu_temps = [
        { name: 'CPU1 Temp', temp: 54.5, status: 'ok' },
        { name: 'CPU2 Temp', temp: 52.0, status: 'ok' }
      ];
      state.fans = [];
      for (let i = 1; i <= 6; i++) {
        state.fans.push({ name: `Fan${i} RPM`, rpm: 4200, speed_pct: 25, status: 'ok' });
      }
      state.all_sensors = [
        { name: 'Inlet Temp', value: '21.0', unit: 'degrees C', status: 'ok', warn_min: '-7.0', warn_max: '42.0', fault_max: '47.0' },
        { name: 'Exhaust Temp', value: '35.5', unit: 'degrees C', status: 'ok', warn_min: 'na', warn_max: '70.0', fault_max: '75.0' },
        { name: 'CPU1 Temp', value: '54.5', unit: 'degrees C', status: 'ok', warn_min: 'na', warn_max: '84.0', fault_max: '90.0' },
        { name: 'CPU2 Temp', value: '52.0', unit: 'degrees C', status: 'ok', warn_min: 'na', warn_max: '84.0', fault_max: '90.0' },
        { name: 'System Board Temp', value: '31.0', unit: 'degrees C', status: 'ok', warn_min: 'na', warn_max: '55.0', fault_max: '60.0' },
        { name: 'Pwr Consumption', value: '188.0', unit: 'Watts', status: 'ok', warn_min: 'na', warn_max: '890.0', fault_max: '950.0' },
        { name: 'Current 1', value: '0.725', unit: 'Amps', status: 'ok', warn_min: 'na', warn_max: 'na', fault_max: 'na' },
        { name: 'Current 2', value: '0.118', unit: 'Amps', status: 'ok', warn_min: 'na', warn_max: 'na', fault_max: 'na' },
        { name: 'Voltage 1', value: '224.000', unit: 'Volts', status: 'ok', warn_min: 'na', warn_max: 'na', fault_max: 'na' },
        { name: 'Fan Redundancy', value: 'Redundant', unit: '', status: 'ok', warn_min: 'na', warn_max: 'na', fault_max: 'na' },
        { name: 'PS Redundancy', value: 'Redundant', unit: '', status: 'ok', warn_min: 'na', warn_max: 'na', fault_max: 'na' },
        { name: 'VBAT Battery', value: '3.100', unit: 'Volts', status: 'ok', warn_min: '2.4', warn_max: '3.6', fault_max: 'na' }
      ];
      for (let i = 1; i <= 6; i++) {
        state.all_sensors.push({
          name: `Fan${i} RPM`,
          value: '4200.000',
          unit: 'RPM',
          status: 'ok',
          warn_min: '720.0',
          warn_max: 'na',
          fault_max: 'na'
        });
      }

      state.cluster_telemetry = state.servers.map((s, idx) => ({
        id: s.id,
        name: s.name,
        ip: s.ip,
        brand: s.brand || 'dell',
        model: s.model || '通用服务器',
        serial: s.serial || '',
        connected: true,
        mode: s.mode || 'auto',
        max_cpu_temp: 52.5 + (idx * 4.2),
        inlet_temp: 21.0 + (idx * 0.5),
        avg_fan_rpm: 3800 + (idx * 450),
        fan_target_pct: 25 + (idx * 5),
        latency_ms: 10 + (idx * 2),
        cpu_temps: state.cpu_temps,
        fans: state.fans,
        all_sensors: state.all_sensors,
        power: {
          total_watts: 188.0 + (idx * 35.0),
          ps1: { name: 'PS1', watts: 162.0, current: 0.725, status: 'ok', online: true, installed: true },
          ps2: { name: 'PS2', watts: 26.0, current: 0.118, status: 'ok', online: true, installed: true }
        }
      }));
    } else {
      // 退出演示模式：恢复用户真实的配置，不保留任何演示的主机与虚拟机
      if (state.config?.servers) {
        state.servers = state.config.servers;
      }
      if (state.config?.system_servers) {
        state.system_servers = state.config.system_servers;
      }
      state.connected = false;
      state.max_cpu_temp = null;
      state.inlet_temp = null;
      state.current_target_speed = null;
      state.cpu_temps = [];
      state.fans = [];
      state.cluster_telemetry = (state.servers || []).map(s => ({
        id: s.id,
        name: s.name,
        ip: s.ip,
        model: s.model || 'Dell PowerEdge',
        connected: false
      }));
      state.system_servers = (state.system_servers || []).map(s => ({
        ...s,
        connected: false,
        last_error: `离线 (实机测活未通)`,
        cpu_pct: 0,
        mem_pct: 0,
        swap_pct: 0,
        disk_pct: 0
      }));
    }
    applyStatusData({
      connected: state.connected,
      demo_mode: state.demo_mode,
      max_cpu_temp: state.max_cpu_temp,
      inlet_temp: state.inlet_temp,
      current_target_speed: state.current_target_speed,
      latency_ms: state.latency_ms,
      cpu_temps: state.cpu_temps,
      fans: state.fans,
      all_sensors: state.all_sensors,
      power: {
        total_watts: 188.0,
        ps1: { name: 'PS1', watts: 162.0, current: 0.725, status: 'ok', online: true, installed: true },
        ps2: { name: 'PS2', watts: 26.0, current: 0.118, status: 'ok', online: true, installed: true }
      },
      cluster_telemetry: state.cluster_telemetry,
      system_servers: state.system_servers,
      config: {
        ...state.config,
        servers: state.servers,
        system_servers: state.system_servers
      }
    });
    return { success: true, demo_mode: enable };
  }

  if (method === 'get_status') {
    // If in demo mode client-side simulation, synthesize smooth wave telemetry fluctuations
    if (state.demo_mode) {
      if (!window._client_demo_tick) window._client_demo_tick = 0;
      window._client_demo_tick++;
      const dtick = window._client_demo_tick;

      state.system_servers = (state.system_servers || []).map((s, idx) => {
        const isBound = bool => Boolean(bool);
        const bound = isBound(s.node_id);
        const baseCpu = bound ? 32.0 : 24.0;
        const wave = Math.sin((dtick * 0.3) + idx * 1.5);
        const cpu_p = Math.max(5.0, Math.min(95.0, Math.round((baseCpu + 14.0 * wave + (Math.random() * 2 - 1)) * 10) / 10));
        const mem_p = Math.max(15.0, Math.min(90.0, Math.round(((bound ? 45.0 : 35.0) + 4.0 * Math.sin(dtick * 0.1 + idx)) * 10) / 10));
        const ramTotal = s.mem_total_gb || (bound ? 64.0 : 32.0);
        return {
          ...s,
          connected: true,
          cpu_pct: cpu_p,
          mem_pct: mem_p,
          mem_used_gb: Math.round(ramTotal * mem_p / 100 * 10) / 10,
          latency_ms: Math.floor(Math.random() * 10) + 4
        };
      });
    }

    return {
      success: true,
      data: {
        connected: state.connected,
        demo_mode: state.demo_mode,
        max_cpu_temp: state.max_cpu_temp,
        inlet_temp: state.inlet_temp,
        current_target_speed: state.current_target_speed,
        latency_ms: state.latency_ms,
        cpu_temps: state.cpu_temps,
        fans: state.fans,
        all_sensors: state.all_sensors,
        power: state.power || {
          total_watts: 188.0,
          ps1: { name: 'PS1', watts: 162.0, current: 0.725, status: 'ok', online: true, installed: true },
          ps2: { name: 'PS2', watts: 26.0, current: 0.118, status: 'ok', online: true, installed: true }
        },
        cluster_telemetry: state.cluster_telemetry,
        system_servers: state.system_servers,
        config: state.config
      }
    };
  }

  if (method === 'set_fan_mode') {
    const mode = args[0];
    const srvId = args[1];
    const srv = state.servers.find(s => s.id === srvId) || state.servers[0];
    if (srv) srv.mode = mode;
    state.mode = mode;
    return { success: true, message: `模式已切换为 ${mode}` };
  }

  if (method === 'set_all_fans_speed') {
    const sp = args[0];
    const srvId = args[1];
    const srv = state.servers.find(s => s.id === srvId) || state.servers[0];
    if (srv) { srv.mode = 'manual'; srv.manual_speed = sp; }
    state.mode = 'manual';
    state.current_target_speed = sp;
    return { success: true, message: `所有风扇转速已设定为 ${sp}%` };
  }

  if (method === 'update_manual_speed_strategy') {
    const sp = args[0];
    let count = 0;
    for (const s of state.servers) {
      if (s.mode === 'manual') {
        s.manual_speed = sp;
        count++;
      }
    }
    state.current_target_speed = sp;
    return { success: true, speed: sp, applied_count: count, message: `手动基准已设为 ${sp}%，已自动同步至 ${count} 台使用手动模式的服务器` };
  }

  if (method === 'update_single_fan_strategy') {
    const idx = args[0];
    const sp = args[1];
    let count = 0;
    for (const s of state.servers) {
      if (s.mode === 'manual') count++;
    }
    return { success: true, fan_index: idx, speed: sp, applied_count: count, message: `通道 #${idx + 1} 已设为 ${sp}%，已自动同步至 ${count} 台使用手动模式的服务器` };
  }

  if (method === 'update_preset_strategy') {
    const pk = args[0];
    let count = 0;
    for (const s of state.servers) {
      if (s.mode === 'preset') {
        s.preset_key = pk;
        count++;
      }
    }
    return { success: true, preset_key: pk, applied_count: count, message: `情景方案已更新，已自动同步至 ${count} 台使用方案模式的服务器` };
  }

  if (method === 'apply_preset') {
    const pk = args[0];
    const srvId = args[1];
    const srv = state.servers.find(s => s.id === srvId) || state.servers[0];
    if (srv) { srv.mode = 'preset'; srv.preset_key = pk; }
    state.mode = 'preset';
    return { success: true, message: `已成功应用预设: ${pk}` };
  }

  if (method === 'update_system_server') {
    const sysId = args[0];
    const updates = args[1] || {};
    const srv = state.system_servers.find(s => s.id === sysId);
    if (srv) {
      Object.assign(srv, updates);
    }
    return { success: true, message: '系统服务器配置已更新' };
  }

  if (method === 'add_system_server') {
    const [name, host, port, username, password, node_id, os_name] = args;
    const newId = `srv_${Date.now().toString(16).slice(-6)}`;
    const newSrv = {
      id: newId,
      name: name || `系统服务器 (${host})`,
      host: host || '127.0.0.1',
      port: port || 22,
      username: username || 'root',
      password: password || '',
      node_id: node_id || '',
      os_name: os_name || '',
      enabled: true,
      connected: true,
      cpu_pct: 20,
      cpu_cores: node_id ? 16 : 8,
      mem_pct: 35,
      mem_used_gb: node_id ? 22.4 : 11.2,
      mem_total_gb: node_id ? 64.0 : 32.0,
      swap_pct: 5,
      swap_used_gb: 0.4,
      swap_total_gb: 8.0,
      disk_pct: 30,
      disk_used_gb: node_id ? 307.2 : 153.6,
      disk_total_gb: node_id ? 1024.0 : 512.0,
      uptime_sec: 86400 * 10,
      hostname: host
    };
    state.system_servers.push(newSrv);
    return { success: true, message: '系统服务器添加成功' };
  }

  if (method === 'delete_system_server') {
    const sysId = args[0];
    state.system_servers = state.system_servers.filter(s => s.id !== sysId);
    return { success: true, message: '系统服务器已移除' };
  }

  if (method === 'control_server_power') {
    const [srvId, action] = args;
    const srv = (state.servers || []).find(s => s.id === srvId);
    const srvName = srv ? srv.name : '服务器';
    const actionMap = {
      'on': '开机 (Power On)',
      'soft': 'ACPI 关机 (Soft Shutdown)',
      'reset': '强制重启 (Reset)',
      'off': '强制断电 (Power Off)'
    };
    return {
      success: true,
      message: `[${srvName}] 模拟 IPMI 电源指令 [${actionMap[action] || action}] 下发成功`
    };
  }

  return { success: true, fallback: true };
}

// Toast notification
function showToast(message, type = 'info') {
  const toast = document.getElementById('toast');
  const msgEl = document.getElementById('toastMessage');
  msgEl.textContent = message;
  toast.className = `apple-toast ${type} show`;
  setTimeout(() => {
    toast.classList.remove('show');
  }, 3200);
}

// ==========================================
// Initialization & Lifecycle
// ==========================================
let isPywebviewReady = false;

function bootstrapApp() {
  initWindowControls();
  initResizeHandles();
  initSidebarTabs();
  initThemeToggle();
  initDashboardControls();
  initProbeToolbar();
  initCurveEditor();
  initFanMatrix();
  initPresetsCatalog();
  initSensorsTab();
  initServerClusterManagement();
  initAlertCenterTab();
  initServerDetailModalEvents();
  initSystemLogsTab();
  initSettingsTab();

  // 1. 初次启动即刻渲染探针矩阵骨架（展示配置中的物理节点），绝不等待异步返回
  renderProbeClusterMatrix();
  renderTitlebarPill();

  // 2. 启动定时状态同步轮询
  startStatusPolling();
}

window.addEventListener('pywebviewready', () => {
  isPywebviewReady = true;
  refreshAllData();
  // 300ms 后再次触发，确保 pywebview 初始化第一帧平滑接收状态快照
  setTimeout(refreshAllData, 300);
});

document.addEventListener('DOMContentLoaded', () => {
  bootstrapApp();
});

function triggerOpenAddHardwareNodeModal() {
  editingServerId = null;
  const formTitle = document.getElementById('serverFormTitle');
  if (formTitle) formTitle.textContent = '添加硬件节点 (IPMI)';
  const srvName = document.getElementById('srvFormName');
  if (srvName) srvName.value = '';
  const brandSel = document.getElementById('srvFormBrand');
  if (brandSel) brandSel.value = 'dell';
  const srvModel = document.getElementById('srvFormModel');
  if (srvModel) srvModel.value = '';
  const serialEl = document.getElementById('srvFormSerial');
  if (serialEl) serialEl.value = '';
  const srvIp = document.getElementById('srvFormIp');
  if (srvIp) srvIp.value = '192.168.1.1';
  const srvUser = document.getElementById('srvFormUser');
  if (srvUser) srvUser.value = 'root';
  const srvPass = document.getElementById('srvFormPassword');
  if (srvPass) srvPass.value = '';
  openHardwareNodeModal();
}

function triggerOpenAddSystemServerModal() {
  editingSysServerId = null;
  const formTitle = document.getElementById('sysServerFormTitle');
  if (formTitle) formTitle.textContent = '添加系统服务器 (SSH 探针)';
  const srvName = document.getElementById('sysSrvFormName');
  if (srvName) srvName.value = '';
  const srvHost = document.getElementById('sysSrvFormHost');
  if (srvHost) srvHost.value = '192.168.1.100';
  const srvPort = document.getElementById('sysSrvFormPort');
  if (srvPort) srvPort.value = '22';
  const srvUser = document.getElementById('sysSrvFormUser');
  if (srvUser) srvUser.value = 'root';
  const srvPass = document.getElementById('sysSrvFormPassword');
  if (srvPass) srvPass.value = '';
  const osEl = document.getElementById('sysSrvFormOsName');
  if (osEl) osEl.value = '';

  // Populate nodes select
  const nodeSel = document.getElementById('sysSrvFormNodeSelect');
  if (nodeSel) {
    let opts = '<option value="">-- 独立服务器 (不绑定任何硬件节点) --</option>';
    (state.servers || []).forEach(n => {
      opts += `<option value="${n.id}">🖥️ 绑定到: ${n.name} (${n.ip})</option>`;
    });
    nodeSel.innerHTML = opts;
  }

  const testResBox = document.getElementById('sysSrvTestResultBox');
  if (testResBox) testResBox.style.display = 'none';
  openSystemServerModal();
}

// Window Traffic Lights & Controls
function initWindowControls() {
  const btnClose = document.getElementById('btnWindowClose');
  const btnMin = document.getElementById('btnWindowMin');
  const btnMax = document.getElementById('btnWindowMax');

  if (btnClose) {
    btnClose.addEventListener('click', (e) => {
      e.stopPropagation();
      callApi('close_window');
    });
  }
  if (btnMin) {
    btnMin.addEventListener('click', (e) => {
      e.stopPropagation();
      callApi('minimize_window');
    });
  }
  if (btnMax) {
    btnMax.addEventListener('click', (e) => {
      e.stopPropagation();
      callApi('toggle_maximize');
    });
  }

  // Titlebar dragging via robust Pointer Events & IPC bounds moving
  const titlebar = document.getElementById('appMainTitlebar') || document.querySelector('.mac-titlebar');
  if (titlebar) {
    let isDragging = false;
    let startPointerX = 0, startPointerY = 0;
    let startWinX = 0, startWinY = 0;
    let dragRafId = null;
    let latestTargetX = 0, latestTargetY = 0;

    titlebar.addEventListener('pointerdown', async (e) => {
      if (e.target.closest('button') || e.target.closest('.traffic-lights') || e.target.closest('input') || e.target.closest('select')) {
        return;
      }
      if (e.button !== 0) return; // Only primary mouse button

      e.preventDefault();
      try { titlebar.setPointerCapture(e.pointerId); } catch (_) {}
      isDragging = true;
      startPointerX = e.screenX;
      startPointerY = e.screenY;

      const bounds = await callApi('get_window_bounds');
      if (bounds && bounds.x !== undefined) {
        startWinX = bounds.x;
        startWinY = bounds.y;
      } else {
        startWinX = window.screenX;
        startWinY = window.screenY;
      }
    });

    titlebar.addEventListener('pointermove', (e) => {
      if (!isDragging) return;
      const dx = e.screenX - startPointerX;
      const dy = e.screenY - startPointerY;
      latestTargetX = Math.round(startWinX + dx);
      latestTargetY = Math.round(startWinY + dy);

      if (!dragRafId) {
        dragRafId = requestAnimationFrame(async () => {
          dragRafId = null;
          if (isDragging) {
            await callApi('set_window_bounds', latestTargetX, latestTargetY, null, null);
          }
        });
      }
    });

    const finishDrag = (e) => {
      if (isDragging) {
        isDragging = false;
        try { titlebar.releasePointerCapture(e.pointerId); } catch (_) {}
        if (dragRafId) {
          cancelAnimationFrame(dragRafId);
          dragRafId = null;
        }
      }
    };

    titlebar.addEventListener('pointerup', finishDrag);
    titlebar.addEventListener('pointercancel', finishDrag);

    // Double click titlebar to toggle maximize
    titlebar.addEventListener('dblclick', (e) => {
      if (e.target.closest('button') || e.target.closest('.traffic-lights')) return;
      callApi('toggle_maximize');
    });
  }

  // 移除了侧边栏原访问 iDRAC 控制台按钮，关于软件已置于左下角

  const btnDemo = document.getElementById('btnToggleDemo');
  if (btnDemo) {
    btnDemo.addEventListener('click', async () => {
      const nextState = !state.demo_mode;
      state.demo_mode = nextState;
      btnDemo.classList.toggle('active', state.demo_mode);

      showToast(state.demo_mode ? '已开启演示仿真，正在全局生成多节点工况...' : '已关闭仿真，正在恢复用户配置并执行实机测活...', 'info');

      // Call backend API to switch mode and retrieve full snapshot
      const res = await callApi('toggle_demo_mode', nextState);
      if (res && res.data) {
        applyStatusData(res.data);
      } else {
        // Fallback optimistic simulation
        handleClientSideFallback('toggle_demo_mode', nextState);
      }

      // 强制重置探针矩阵的 DOM 缓存指纹，确保即时彻底重绘卡片！
      const container = document.getElementById('probeCardsContainer');
      if (container) {
        container.dataset.matrixStructureKey = '';
      }

      // Refresh current active view immediately
      if (state.activeTab === 'dashboard') {
        if (state.dashboardViewMode === 'detail') {
          renderDashboardMetrics();
          renderSensorsTable();
        } else {
          renderProbeClusterMatrix(true);
        }
      } else if (state.activeTab === 'servers') {
        renderServerManagementList();
      } else if (state.activeTab === 'curve') {
        renderCurveView();
      } else if (state.activeTab === 'alerts') {
        renderAlertsCenter();
      } else if (state.activeTab === 'ops') {
        renderOpsPlatform();
      }
      renderTitlebarPill();
      showToast(state.demo_mode ? '演示仿真已就绪 (包含 Dell/浪潮/华为 多品牌多节点生态与 SN 展示)' : '已恢复用户配置，实机测活完成', 'success');
    });
  }
}

// Window Border Resizing Grips (Pure Frameless Smooth Resizing with ZERO White Border)
function initResizeHandles() {
  const handles = document.querySelectorAll('.resize-handle');
  let isResizing = false;
  let currentDir = '';
  let startPointerX = 0, startPointerY = 0;
  let startWinX = 0, startWinY = 0;
  let startWinW = 0, startWinH = 0;
  const minW = 960, minH = 640;
  let rafId = null;
  let latestBounds = null;

  handles.forEach(handle => {
    handle.addEventListener('pointerdown', async (e) => {
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      isResizing = true;
      currentDir = handle.dataset.dir || 'se';
      startPointerX = e.screenX;
      startPointerY = e.screenY;

      // Query true native window coordinates
      const bounds = await callApi('get_window_bounds');
      if (bounds && bounds.width) {
        startWinX = bounds.x;
        startWinY = bounds.y;
        startWinW = bounds.width;
        startWinH = bounds.height;
      } else {
        startWinX = window.screenX;
        startWinY = window.screenY;
        startWinW = window.outerWidth;
        startWinH = window.outerHeight;
      }

      document.body.classList.add('resizing');
    });

    handle.addEventListener('pointermove', (e) => {
      if (!isResizing) return;
      const dx = e.screenX - startPointerX;
      const dy = e.screenY - startPointerY;

      let newW = startWinW;
      let newH = startWinH;
      let newX = startWinX;
      let newY = startWinY;
      let posChanged = false;

      // Horizontal sizing
      if (currentDir.includes('e')) {
        newW = Math.max(minW, startWinW + dx);
      } else if (currentDir.includes('w')) {
        const potentialW = startWinW - dx;
        if (potentialW >= minW) {
          newW = potentialW;
          newX = startWinX + dx;
        } else {
          newW = minW;
          newX = startWinX + (startWinW - minW);
        }
        posChanged = true;
      }

      // Vertical sizing
      if (currentDir.includes('s')) {
        newH = Math.max(minH, startWinH + dy);
      } else if (currentDir.includes('n')) {
        const potentialH = startWinH - dy;
        if (potentialH >= minH) {
          newH = potentialH;
          newY = startWinY + dy;
        } else {
          newH = minH;
          newY = startWinY + (startWinH - minH);
        }
        posChanged = true;
      }

      latestBounds = {
        x: posChanged ? Math.round(newX) : null,
        y: posChanged ? Math.round(newY) : null,
        w: Math.round(newW),
        h: Math.round(newH)
      };

      if (!rafId) {
        rafId = requestAnimationFrame(() => {
          if (latestBounds) {
            callApi('set_window_bounds', latestBounds.x, latestBounds.y, latestBounds.w, latestBounds.h);
          }
          rafId = null;
        });
      }
    });

    const finishResize = (e) => {
      if (isResizing) {
        isResizing = false;
        try { handle.releasePointerCapture(e.pointerId); } catch (_) {}
        document.body.classList.remove('resizing');
      }
    };

    handle.addEventListener('pointerup', finishResize);
    handle.addEventListener('pointercancel', finishResize);
  });
}

// Sidebar Navigation
function initSidebarTabs() {
  const navItems = document.querySelectorAll('.mac-sidebar .nav-item');
  navItems.forEach(item => {
    item.addEventListener('click', () => {
      const tabId = item.dataset.tab;
      if (!tabId) return;
      navItems.forEach(n => n.classList.remove('active'));
      item.classList.add('active');

      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      const activePane = document.getElementById(`pane-${tabId}`);
      if (activePane) activePane.classList.add('active');
      state.activeTab = tabId;

      if (tabId === 'dashboard') {
        if (state.dashboardViewMode === 'detail') {
          renderDashboardMetrics();
          renderSensorsTable();
        } else {
          renderProbeClusterMatrix();
        }
      }
      if (tabId === 'curve') renderCurveView();
      if (tabId === 'fans' && typeof window.syncManualTabSliders === 'function') window.syncManualTabSliders(true);
      if (tabId === 'presets') initPresetsCatalog();
      if (tabId === 'servers') renderServerManagementList();
      if (tabId === 'alerts') renderAlertsCenter();
      if (tabId === 'ops') renderOpsPlatform();
      if (tabId === 'logs') refreshSystemLogs(true);
      if (tabId === 'settings') populatePreferencesForm();
    });
  });
}

// Dark/Light Theme
function initThemeToggle() {
  const btn = document.getElementById('btnThemeToggle');
  btn.addEventListener('click', () => {
    document.body.classList.toggle('theme-dark');
  });
}

// ==========================================
// Dashboard View & Polling Controls
// ==========================================
function initDashboardControls() {
  // Back to Cluster Button
  const btnBack = document.getElementById('btnBackToCluster');
  if (btnBack) {
    btnBack.addEventListener('click', () => {
      setDashboardView('probe');
      const prefDefaultView = document.getElementById('prefDefaultView');
      if (prefDefaultView) prefDefaultView.value = 'probe';
      callApi('set_config', { dashboard_view_mode: 'probe' });
    });
  }

  // Focused Server Dropdown
  const focusedSelect = document.getElementById('focusedServerSelect');
  if (focusedSelect) {
    focusedSelect.addEventListener('change', async (e) => {
      const srvId = e.target.value;
      await switchActiveServer(srvId);
    });
  }

  // Mode Segmented Control
  const modeBtns = document.querySelectorAll('#modeSegmentedControl .seg-btn');
  modeBtns.forEach(btn => {
    btn.addEventListener('click', async () => {
      const targetMode = btn.dataset.mode;
      const res = await callApi('set_fan_mode', targetMode);
      if (res && res.success) {
        state.mode = targetMode;
        updateModeSegmentedUI(targetMode);
        showToast(res.message || `模式已切换为 ${targetMode}`, 'success');
      } else {
        showToast(res.message || '切换模式失败', 'error');
      }
    });
  });

  // Slider & Restore
  const slider = document.getElementById('globalFanSlider');
  const sliderBadge = document.getElementById('sliderSpeedValue');
  slider.addEventListener('input', (e) => {
    sliderBadge.textContent = `${e.target.value}%`;
  });

  document.getElementById('btnApplyGlobalSpeed').addEventListener('click', async () => {
    const sp = parseInt(slider.value, 10);
    const srvId = state.activeServer?.id;
    const res = await callApi('set_all_fans_speed', sp, srvId);
    if (res && res.success) {
      showToast(res.message || `已应用调速 ${sp}%`, 'success');
      if (state.activeServer) {
        state.activeServer.mode = 'manual';
        state.activeServer.manual_speed = sp;
      }
      await refreshAllData();
    } else {
      showToast(res?.message || res?.error || '风扇调速失败', 'error');
    }
  });

  document.getElementById('btnRestoreDellAuto').addEventListener('click', async () => {
    const srvId = state.activeServer?.id;
    const res = await callApi('set_fan_mode', 'auto', srvId);
    if (res && res.success) {
      showToast(res.message || '已恢复 Dell 原厂动态自动控温', 'success');
      if (state.activeServer) {
        state.activeServer.mode = 'auto';
      }
      await refreshAllData();
    } else {
      showToast(res?.message || res?.error || '恢复原厂模式失败', 'error');
    }
  });

  const btnRefreshSens = document.getElementById('btnRefreshSensors');
  if (btnRefreshSens) {
    btnRefreshSens.addEventListener('click', async () => {
      showToast('正在中断旧会话并强制重连此节点...', 'info');
      await callApi('force_reconnect');
      await refreshAllData();
      showToast('已重连并触发传感器采样');
    });
  }

  // Offline banner actions (safe guard if element exists)
  const btnOfflineDemo = document.getElementById('btnOfflineTryDemo');
  if (btnOfflineDemo) {
    btnOfflineDemo.addEventListener('click', async () => {
      document.getElementById('btnToggleDemo').click();
    });
  }
  const btnOfflineCfg = document.getElementById('btnOfflineEditConfig');
  if (btnOfflineCfg) {
    btnOfflineCfg.addEventListener('click', () => {
      document.querySelector('.nav-item[data-tab="servers"]').click();
    });
  }

  // Probe Layout Switcher (方块 Grid / 长条 Row)
  const probeLayoutSwitch = document.getElementById('probeLayoutSwitch');
  if (probeLayoutSwitch) {
    probeLayoutSwitch.querySelectorAll('.seg-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const style = btn.dataset.style;
        switchProbeLayoutStyle(style);
      });
    });
  }
}

function setDashboardView(viewMode) {
  state.dashboardViewMode = viewMode;
  const probeView = document.getElementById('probeClusterView');
  const detailView = document.getElementById('focusedDetailView');
  const dashHero = document.getElementById('dashHeroHeader');
  const mainPane = document.querySelector('.mac-content-pane');

  if (viewMode === 'detail') {
    if (probeView) probeView.style.display = 'none';
    if (detailView) detailView.style.display = 'block';
    if (dashHero) dashHero.style.display = 'none'; // Hide redundant huge dashboard hero when diving into single machine detail
    if (mainPane) mainPane.scrollTop = 0;
    renderDashboardMetrics();
    renderSensorsTable();
  } else {
    if (probeView) probeView.style.display = 'block';
    if (detailView) detailView.style.display = 'none';
    if (dashHero) dashHero.style.display = 'flex';
    if (mainPane) mainPane.scrollTop = 0;
  }
}

function updateModeSegmentedUI(mode) {
  // Global switcher removed in favor of per-server controls
}

async function switchActiveServer(srvId) {
  const srv = state.servers.find(s => s.id === srvId) || state.servers[0];
  if (!srv) return;
  state.activeServer = srv;
  const tel = state.cluster_telemetry.find(t => t.id === srvId);
  if (tel) {
    state.connected = !!tel.connected;
    state.latency_ms = tel.latency_ms || 0;
    state.mode = tel.mode || srv.mode || 'auto';
    state.max_cpu_temp = tel.max_cpu_temp;
    state.inlet_temp = tel.inlet_temp;
    state.cpu_temps = tel.cpu_temps || [];
    state.fans = tel.fans || [];
    state.all_sensors = tel.all_sensors || [];
    state.power = tel.power || { total_watts: null, ps1: {}, ps2: {} };
    state.current_target_speed = tel.fan_target_pct;
    if (!state.connected) {
      state.max_cpu_temp = null;
      state.inlet_temp = null;
      state.cpu_temps = [];
      state.fans = [];
      state.all_sensors = [];
      state.power = { total_watts: null, ps1: {}, ps2: {} };
      state.current_target_speed = null;
    }
  }
  const focusedSelect = document.getElementById('focusedServerSelect');
  if (focusedSelect) focusedSelect.value = srvId;
  // 零延迟即时重绘，瞬间呈现切换结果！
  renderDashboardMetrics();
  renderSensorsTable();
  showToast(`已即刻聚焦服务器: ${srv.name}`, 'success');

  // 后台无阻塞通知 Python 切换受控记录
  callApi('switch_server', srvId).then(res => {
    if (res && res.success && res.data) {
      applyStatusData(res.data);
    }
  });
}

// ==========================================
// Status Polling Loop (Decoupled UI vs Data Fetch)
// ==========================================
let isFetchingNow = false;

function startStatusPolling() {
  if (pollIntervalTimer) clearInterval(pollIntervalTimer);
  
  // 启动即刻渲染并拉取最新状态快照
  refreshAllData();

  // 界面 UI 画面刷新频率：默认 1 秒平滑重绘（用户可在设置中自定义 1~10s）
  const uiSec = Math.max(1, Math.min(10, state.uiRefreshSec || 1));
  pollIntervalTimer = setInterval(async () => {
    await refreshAllData();
  }, uiSec * 1000);
}

async function refreshAllData() {
  if (isFetchingNow) return;
  isFetchingNow = true;
  try {
    // 从底层内存快照读取状态（耗时不到 1ms，绝不阻塞网络）
    const res = await callApi('get_status');
    if (res && res.success && res.data) {
      applyStatusData(res.data);
    }
  } catch (err) {
    // 后台繁忙或异常时，平滑保留当前内存旧数据继续渲染，避免界面卡死或闪烁空白
  } finally {
    isFetchingNow = false;
  }
}

function applyStatusData(d) {
  if (!d) return;
  state.connected = !!d.connected;
  state.latency_ms = d.latency_ms;
  state.mode = d.mode || state.mode;
  state.current_target_speed = d.current_target_speed;
  state.max_cpu_temp = d.max_cpu_temp;
  state.inlet_temp = d.inlet_temp;
  state.cpu_temps = d.cpu_temps || [];
  state.fans = d.fans || [];
  state.all_sensors = d.all_sensors || [];
  state.power = d.power || { total_watts: null, ps1: {}, ps2: {} };

  // 严格要求：如果节点被判断离线，则不显示旧的虚假数值
  if (!state.connected) {
    state.max_cpu_temp = null;
    state.inlet_temp = null;
    state.cpu_temps = [];
    state.fans = [];
    state.all_sensors = [];
    state.current_target_speed = null;
    state.power = { total_watts: null, ps1: {}, ps2: {} };
  }
  state.safety_triggered = d.safety_triggered;
  state.demo_mode = d.demo_mode;
  state.autostart_active = d.autostart_active;
  state.cluster_telemetry = d.cluster_telemetry || [];
  if (d.system_servers) state.system_servers = d.system_servers;
  if (d.subsystems) state.subsystems = d.subsystems;
  if (d.subsystem_poll_sec) {
    state.subsystem_poll_sec = d.subsystem_poll_sec;
    const qPoll = document.getElementById('quickSubPollInput');
    if (qPoll && document.activeElement !== qPoll) qPoll.value = d.subsystem_poll_sec;
    const prefPoll = document.getElementById('prefSubsystemPollRate');
    if (prefPoll && document.activeElement !== prefPoll) prefPoll.value = d.subsystem_poll_sec;
  }
  if (d.alert_history) state.alert_history = d.alert_history;
  if (d.alert_config) state.alert_config = d.alert_config;

  // Update Nav Alert Badge
  const navAlertBadge = document.getElementById('navAlertBadge');
  if (navAlertBadge) {
    const unreadCount = (state.alert_history || []).length;
    if (unreadCount > 0) {
      navAlertBadge.style.display = 'inline-block';
      navAlertBadge.textContent = unreadCount > 99 ? '99+' : unreadCount;
    } else {
      navAlertBadge.style.display = 'none';
    }
  }

  if (d.config) {
    state.config = d.config;
    if (d.config.curve_nodes && state.activeTab !== 'curve') {
      state.curve_nodes = d.config.curve_nodes;
    }
    if (d.config.servers) {
      state.servers = d.config.servers;
      if (state.demo_mode) {
        state.demo_servers = d.config.servers;
      }
    }
    if (d.config.active_server) state.activeServer = d.config.active_server;
    if (d.config.ipmi?.probe_layout_style && !state.probeLayoutStyleUserModified) {
      state.probeLayoutStyle = d.config.ipmi.probe_layout_style;
    }
    if (d.config.ipmi?.auto_refresh_sec) {
      const parsedSec = parseInt(d.config.ipmi.auto_refresh_sec, 10);
      if (parsedSec && parsedSec !== state.autoRefreshSec) {
        state.autoRefreshSec = parsedSec;
        startStatusPolling();
      }
    }
    if (typeof window.syncManualTabSliders === 'function') {
      window.syncManualTabSliders();
    }
  }

  // Ensure demo state takes absolute precedence during demo mode
  if (state.demo_mode) {
    if (d.system_servers && d.system_servers.length > 0) {
      state.system_servers = d.system_servers;
      state.demo_system_servers = d.system_servers;
    } else if (state.demo_system_servers && state.demo_system_servers.length > 0) {
      state.system_servers = state.demo_system_servers;
    }
    if (state.demo_servers && state.demo_servers.length > 0) {
      state.servers = state.demo_servers;
    }
  }

  // Update Titlebar Pill (Always safe, outside content pane)
  renderTitlebarPill();

  // STRICT TAB ISOLATION:
  // ONLY render and mutate DOM for tabs that are actually visible!
  // When user is on any other tab (Curve, Manual Fans, Presets, Servers, Preferences, About):
  // The background polling DOES NOT TOUCH THE DOM AT ALL.
  if (state.activeTab === 'dashboard') {
    if (state.dashboardViewMode === 'detail') {
      renderDashboardMetrics();
      renderSensorsTable();
    } else {
      renderProbeClusterMatrix();
    }
  } else if (state.activeTab === 'alerts') {
    renderAlertsCenter();
  } else if (state.activeTab === 'logs') {
    refreshSystemLogs(false);
  }
}

function renderTitlebarPill() {
  const demoBtn = document.getElementById('btnToggleDemo');

  const nodeTotal = state.servers.length;
  const nodeOnlineCount = (state.cluster_telemetry || []).filter(t => t.connected).length;

  const serverTotal = (state.system_servers || []).length;
  const serverOnlineCount = (state.system_servers || []).filter(s => s.connected).length;

  if (demoBtn) {
    demoBtn.classList.toggle('active', state.demo_mode);
  }

  // 1. 物理 IPMI 节点在线胶囊
  const nodePill = document.getElementById('nodeConnectionPill');
  const nodeDot = document.getElementById('nodeStatusDot');
  const nodeText = document.getElementById('nodeConnectionText');
  if (nodePill && nodeText) {
    if (nodeOnlineCount > 0) {
      nodePill.className = 'connection-pill connected';
      nodeText.textContent = `节点: ${nodeOnlineCount}/${nodeTotal} 在线`;
      if (nodeDot) nodeDot.style.background = 'var(--system-green)';
    } else {
      nodePill.className = 'connection-pill disconnected';
      nodeText.textContent = `节点: 0/${nodeTotal} 离线`;
      if (nodeDot) nodeDot.style.background = 'var(--system-orange)';
    }
  }

  // 2. Linux 系统服务器在线胶囊
  const srvPill = document.getElementById('serverConnectionPill');
  const srvDot = document.getElementById('serverStatusDot');
  const srvText = document.getElementById('serverConnectionText');
  if (srvPill && srvText) {
    if (serverOnlineCount > 0) {
      srvPill.className = 'connection-pill connected';
      srvText.textContent = `服务器: ${serverOnlineCount}/${serverTotal} 在线`;
      if (srvDot) srvDot.style.background = 'var(--system-green)';
    } else {
      srvPill.className = 'connection-pill disconnected';
      srvText.textContent = `服务器: 0/${serverTotal} 离线`;
      if (srvDot) srvDot.style.background = 'var(--system-orange)';
    }
  }
}

// Requirement 1: 首页未连接服务器时不显示具体数值，展示优雅占位符与未连接提示
function renderDashboardMetrics() {
  const isOnline = state.connected;
  const offlineBanner = document.getElementById('offlineHeroBanner');
  const offlineIp = document.getElementById('offlineServerIpTag');
  const offlinePoll = document.getElementById('offlinePollSecTag');
  const dashRefreshBadge = document.getElementById('dashRefreshRateBadge');
  const statusBadge = document.getElementById('detailServerStatusBadge');
  
  if (offlineIp) offlineIp.textContent = state.activeServer?.ip || '192.168.1.1';
  if (offlinePoll) offlinePoll.textContent = state.autoRefreshSec;
  if (dashRefreshBadge) dashRefreshBadge.textContent = `${state.autoRefreshSec}s`;
  if (statusBadge) {
    if (isOnline) {
      statusBadge.className = 'badge badge-normal';
      statusBadge.textContent = '● 联机受控';
    } else {
      statusBadge.className = 'badge badge-warning';
      statusBadge.textContent = '● 离线待联';
    }
  }

  if (offlineBanner) {
    if (isOnline) {
      offlineBanner.classList.remove('visible');
    } else {
      offlineBanner.classList.add('visible');
    }
  }

  // Numbers & text values:
  // 如果节点在线但数据正在初次获取中，优雅显示「获取中...」；若确认彻底离线，严格显示 '--'
  const isConnecting = state.connected === null || state.connected === undefined;
  const isOffline = state.connected === false;
  const placeholder = isOffline ? '--' : '<span class="loading-placeholder">获取中...</span>';

  const maxCpu = (isOnline && state.max_cpu_temp !== null) ? state.max_cpu_temp : placeholder;
  const inlet = (isOnline && state.inlet_temp !== null) ? state.inlet_temp : placeholder;
  const targetPct = (isOnline && state.current_target_speed !== null) ? state.current_target_speed : placeholder;

  const maxCpuEl = document.getElementById('dashMaxCpuTemp');
  if (maxCpuEl) maxCpuEl.innerHTML = maxCpu;
  const inletTempEl = document.getElementById('dashInletTemp');
  if (inletTempEl) inletTempEl.innerHTML = inlet;

  document.getElementById('dashInletVal').innerHTML = (inlet !== '--' && inlet !== placeholder) ? `${inlet} °C` : (isOffline ? '-- °C' : '<span class="loading-placeholder">获取中...</span>');
  const exhaustEl = document.getElementById('dashExhaustVal');
  if (exhaustEl) {
    const exhaustSensor = (state.all_sensors || []).find(s => s.name.toLowerCase().includes('exhaust'));
    exhaustEl.innerHTML = (isOnline && exhaustSensor && exhaustSensor.value) ? `${exhaustSensor.value} °C` : (isOffline ? '-- °C' : '<span class="loading-placeholder">获取中...</span>');
  }
  const fanTargetEl = document.getElementById('dashFanTargetPct');
  if (fanTargetEl) fanTargetEl.innerHTML = targetPct;

  // Power Consumption Metrics (总功耗直接获取，PS1/PS2 单路按电流*电压计算展示，单电源设备隐藏第二路)
  const powerInfo = state.power || {};
  const totalPower = (isOnline && powerInfo.total_watts !== null && powerInfo.total_watts !== undefined) ? powerInfo.total_watts : placeholder;
  const totalPowerEl = document.getElementById('dashTotalPower');
  if (totalPowerEl) totalPowerEl.innerHTML = totalPower;

  const ps1Box = document.getElementById('dashPs1Container');
  const ps1ValEl = document.getElementById('dashPs1Val');
  const ps1LblEl = document.getElementById('dashPs1Label');
  if (powerInfo.ps1) {
    const ps1Installed = powerInfo.ps1.installed !== false;
    if (ps1Box) ps1Box.style.display = ps1Installed ? 'flex' : 'none';
    if (ps1ValEl) {
      let ps1w = '-- W';
      if (isOnline && powerInfo.ps1.watts !== null && powerInfo.ps1.watts !== undefined) {
        const extraCurrent = powerInfo.ps1.current ? ` (${powerInfo.ps1.current}A)` : '';
        ps1w = `${powerInfo.ps1.watts} W${extraCurrent}`;
      }
      ps1ValEl.textContent = ps1w;
      ps1ValEl.style.color = (powerInfo.ps1.online) ? 'var(--system-green)' : 'var(--text-tertiary)';
    }
    if (ps1LblEl) ps1LblEl.textContent = `PS1 电源`;
  }

  const ps2Box = document.getElementById('dashPs2Container');
  const ps2ValEl = document.getElementById('dashPs2Val');
  const ps2LblEl = document.getElementById('dashPs2Label');
  if (powerInfo.ps2) {
    const ps2Installed = powerInfo.ps2.installed === true && (powerInfo.ps2.watts !== null || powerInfo.ps2.online === true);
    // 单电源设备：若未安装或获取不到数据，则不显示 PS2 项目
    if (ps2Box) ps2Box.style.display = ps2Installed ? 'flex' : 'none';
    if (ps2ValEl && ps2Installed) {
      let ps2w = '-- W';
      if (isOnline && powerInfo.ps2.watts !== null && powerInfo.ps2.watts !== undefined) {
        const extraCurrent = powerInfo.ps2.current ? ` (${powerInfo.ps2.current}A)` : '';
        ps2w = `${powerInfo.ps2.watts} W${extraCurrent}`;
      }
      ps2ValEl.textContent = ps2w;
      ps2ValEl.style.color = (powerInfo.ps2.online) ? 'var(--system-green)' : 'var(--text-tertiary)';
    }
    if (ps2LblEl) ps2LblEl.textContent = `PS2 电源`;
  }

  // CPU Cores Sub-list (智能多维度模糊匹配，兼容不同品牌命名)
  const cpu1 = state.cpu_temps.find(c => {
    const nl = c.name.toLowerCase();
    return nl.includes('cpu1') || nl.includes('cpu 1') || nl.includes('proc 1') || nl.includes('p1') || nl.includes('processor 1');
  }) || (state.cpu_temps.length > 0 ? state.cpu_temps[0] : null);

  const cpu2 = state.cpu_temps.find(c => {
    const nl = c.name.toLowerCase();
    return nl.includes('cpu2') || nl.includes('cpu 2') || nl.includes('proc 2') || nl.includes('p2') || nl.includes('processor 2');
  }) || (state.cpu_temps.length > 1 ? state.cpu_temps[1] : null);

  document.getElementById('dashCpu1Temp').textContent = (isOnline && cpu1) ? `${cpu1.temp} °C` : '-- °C';
  document.getElementById('dashCpu2Temp').textContent = (isOnline && cpu2) ? `${cpu2.temp} °C` : '-- °C';

  // Badge status
  const cpuBadge = document.getElementById('cpuTempBadge');
  if (!isOnline) {
    cpuBadge.className = 'badge badge-warning';
    cpuBadge.textContent = '等待采集';
  } else if (state.max_cpu_temp >= 78) {
    cpuBadge.className = 'badge badge-danger';
    cpuBadge.textContent = '核心偏高';
  } else {
    cpuBadge.className = 'badge badge-normal';
    cpuBadge.textContent = '工况优良';
  }

  // Safety Banner
  const safetyBanner = document.getElementById('safetyAlertBanner');
  safetyBanner.style.display = (isOnline && state.safety_triggered) ? 'flex' : 'none';

  // Fan RPM average & animation
  const avgRpmEl = document.getElementById('dashAverageRpm');
  const blades = document.getElementById('turbineBlades');
  if (isOnline && state.fans.length > 0) {
    const avgRpm = Math.round(state.fans.reduce((acc, f) => acc + f.rpm, 0) / state.fans.length);
    avgRpmEl.textContent = `平均 ${avgRpm} RPM`;
    const duration = Math.max(0.4, 4.0 - (state.current_target_speed / 100) * 3.5);
    blades.style.animationDuration = `${duration.toFixed(2)}s`;
  } else {
    avgRpmEl.textContent = '平均 -- RPM';
    blades.style.animationDuration = '0s'; // stop rotation when disconnected
  }

  // Render 6 Fan Cards
  renderFanCardsGrid(isOnline);

  // Sync focusedServerSelect dropdown options
  renderFocusedServerSelect();
}

function renderFocusedServerSelect() {
  const sel = document.getElementById('focusedServerSelect');
  if (!sel || state.servers.length === 0) return;
  const currentVal = sel.value;
  sel.innerHTML = state.servers.map(s => `
    <option value="${s.id}" ${s.id === state.activeServer?.id ? 'selected' : ''}>
      ${s.name} (${s.ip})
    </option>
  `).join('');
}

function renderFanCardsGrid(isOnline) {
  const container = document.getElementById('fansCardsContainer');
  if (!container) return;
  
  const existingMiniCards = container.querySelectorAll('.fan-mini-card');
  if (existingMiniCards.length !== 6) {
    let html = '';
    for (let i = 1; i <= 6; i++) {
      html += `
        <div class="fan-mini-card" id="fanMiniCard_${i}">
          <div class="fan-card-name">风扇 #${i}</div>
          <div class="fan-card-rpm">-- RPM</div>
          <div class="fan-card-pct">--%</div>
          <div class="fan-mini-bar">
            <div class="fan-mini-bar-fill" style="width: 0%"></div>
          </div>
        </div>
      `;
    }
    container.innerHTML = html;
  }

  for (let i = 1; i <= 6; i++) {
    const card = document.getElementById(`fanMiniCard_${i}`);
    if (!card) continue;
    const fan = state.fans.find(f => f.name.toLowerCase().includes(`fan${i}`) || f.name.toLowerCase().includes(`fan ${i}`));
    const rpmText = (isOnline && fan) ? `${fan.rpm} RPM` : '-- RPM';
    const pct = (isOnline && fan) ? fan.speed_pct : 0;
    const pctText = (isOnline && fan) ? `${pct}%` : '--%';

    const rpmEl = card.querySelector('.fan-card-rpm');
    if (rpmEl) rpmEl.textContent = rpmText;

    const pctEl = card.querySelector('.fan-card-pct');
    if (pctEl) pctEl.textContent = pctText;

    const fillEl = card.querySelector('.fan-mini-bar-fill');
    if (fillEl) fillEl.style.width = `${pct}%`;
  }
}

// Requirement 3: 探针矩阵视图 (Probe Cluster Matrix) - 支持方块/长条双样式、状态筛选、多选与批量温控下发
function getVisibleServers() {
  const filter = state.probeFilter || 'all';
  return state.servers.filter(srv => {
    const telemetry = state.cluster_telemetry.find(t => t.id === srv.id) || {};
    const isConn = !!telemetry.connected;
    const mode = telemetry.mode || srv.mode || 'auto';
    if (filter === 'online') return isConn;
    if (filter === 'offline') return !isConn;
    if (filter === 'auto') return mode === 'auto';
    if (filter === 'dynamic') return mode === 'dynamic';
    if (filter === 'manual') return mode === 'manual';
    if (filter === 'preset') return mode === 'preset';
    return true;
  });
}

function initProbeToolbar() {
  // Concept View Segment (全景总览 / 硬件节点 / 系统服务器)
  const conceptBtns = document.querySelectorAll('#conceptViewSegment .seg-btn');
  conceptBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      conceptBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.conceptView = btn.dataset.concept || 'overview';
      renderProbeClusterMatrix();
    });
  });

  const filterPills = document.querySelectorAll('#probeFilterPills .filter-pill');
  filterPills.forEach(pill => {
    pill.addEventListener('click', () => {
      filterPills.forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      state.probeFilter = pill.dataset.filter || 'all';
      renderProbeClusterMatrix();
    });
  });

  const chkAll = document.getElementById('chkSelectAllServers');
  if (chkAll) {
    chkAll.addEventListener('change', (e) => {
      const isChecked = e.target.checked;
      const visible = getVisibleServers();
      if (isChecked) {
        visible.forEach(s => state.selectedServerIds.add(s.id));
      } else {
        state.selectedServerIds.clear();
      }
      updateBatchSelectionUI();
    });
  }

  document.getElementById('btnBatchSetAuto')?.addEventListener('click', () => batchApplyThermalMode('auto'));
  document.getElementById('btnBatchSetDynamic')?.addEventListener('click', () => batchApplyThermalMode('dynamic'));
  document.getElementById('btnBatchSetManual')?.addEventListener('click', () => batchApplyThermalMode('manual'));
  document.getElementById('btnBatchSetPreset')?.addEventListener('click', () => batchApplyThermalMode('preset'));
  document.getElementById('btnBatchClearSelection')?.addEventListener('click', () => {
    state.selectedServerIds.clear();
    const chkAll = document.getElementById('chkSelectAllServers');
    if (chkAll) chkAll.checked = false;
    updateBatchSelectionUI();
  });
}

window.toggleServerSelection = function(srvId, isChecked) {
  if (isChecked) {
    state.selectedServerIds.add(srvId);
  } else {
    state.selectedServerIds.delete(srvId);
  }
  updateBatchSelectionUI();
};

function updateBatchSelectionUI() {
  const bar = document.getElementById('batchActionBar');
  const countEl = document.getElementById('batchSelectedCount');
  const count = state.selectedServerIds.size;
  if (bar) bar.style.display = count > 0 ? 'flex' : 'none';
  if (countEl) countEl.textContent = count;

  const visible = getVisibleServers();
  const chkAll = document.getElementById('chkSelectAllServers');
  if (chkAll) {
    chkAll.checked = visible.length > 0 && visible.every(s => state.selectedServerIds.has(s.id));
  }

  document.querySelectorAll('.probe-checkbox[data-srv-id]').forEach(chk => {
    chk.checked = state.selectedServerIds.has(chk.dataset.srvId);
  });
}

window.batchApplyThermalMode = async function(mode) {
  if (state.selectedServerIds.size === 0) {
    showToast('请先勾选需要批量配置的服务器', 'warning');
    return;
  }
  const srvIds = Array.from(state.selectedServerIds);
  const modeNames = { 'auto': '原厂托管', 'dynamic': '动态曲线', 'manual': '手动固定', 'preset': '情景方案' };
  const label = modeNames[mode] || mode;

  showToast(`正在对选中的 ${srvIds.length} 台服务器批量下发 ${label}...`, 'info');

  let successCount = 0;
  const globalSp = parseInt(state.config?.ipmi?.manual_speed, 10);
  const globalPk = state.config?.ipmi?.preset_key || 'silent';
  for (const srvId of srvIds) {
    let res;
    if (mode === 'manual') {
      const srv = state.servers.find(s => s.id === srvId);
      const sp = srv?.manual_speed || (Number.isFinite(globalSp) && globalSp > 0 ? globalSp : 25);
      res = await callApi('set_all_fans_speed', sp, srvId);
    } else if (mode === 'preset') {
      const srv = state.servers.find(s => s.id === srvId);
      const pk = srv?.preset_key || globalPk;
      res = await callApi('apply_preset', pk, srvId);
    } else {
      res = await callApi('set_fan_mode', mode, srvId);
    }
    if (res && res.success) {
      successCount++;
      const srv = state.servers.find(s => s.id === srvId);
      if (srv) srv.mode = mode;
    }
  }

  showToast(`已成功将 ${successCount}/${srvIds.length} 台服务器统一切换为 ${label}！`, 'success');
  await refreshAllData();
};

function renderProbeClusterMatrix(forceRebuild = false) {
  const countBadge = document.getElementById('probeClusterCount');
  const container = document.getElementById('probeCardsContainer');
  if (!container) return;

  const concept = state.conceptView || 'overview';
  const visibleNodes = getVisibleServers();
  const allSysServers = state.system_servers || [];

  // Update layout switch UI buttons
  const layoutStyle = state.probeLayoutStyle || state.config?.ipmi?.probe_layout_style || 'compact_grid';
  container.className = `probe-cards-grid ${layoutStyle === 'compact_row' ? 'compact-row' : 'compact-grid'}`;
  const switchBtns = document.querySelectorAll('#probeLayoutSwitch .seg-btn');
  switchBtns.forEach(btn => {
    btn.classList.toggle('active', btn.dataset.style === layoutStyle);
  });

  // Calculate structure fingerprint: only rebuild DOM if servers/nodes membership, modes, or view concepts change!
  const nodeIdsStr = visibleNodes.map(n => `${n.id}:${n.brand || ''}:${n.serial || ''}:${n.mode || 'auto'}`).join(',');
  const sysIdsStr = allSysServers.map(s => `${s.id}:${s.node_id || ''}`).join(',');
  const currentStructureKey = `${concept}::${layoutStyle}::${nodeIdsStr}::${sysIdsStr}`;

  if (!forceRebuild && container.dataset.matrixStructureKey === currentStructureKey) {
    // Fast path: In-place DOM update! Retains existing DOM nodes so CSS transitions smoothly animate!
    updateOverviewCardTelemetry(visibleNodes, allSysServers);
    updateBatchSelectionUI();
    return;
  }

  container.dataset.matrixStructureKey = currentStructureKey;

  // =========================================================================
  // VIEW MODE 1: 全景总览 (OVERVIEW)
  // 展示节点及其所绑定的服务器；对于未绑定任何节点的服务器，以独立卡片形式直接呈现！
  // =========================================================================
  if (concept === 'overview') {
    if (countBadge) {
      countBadge.textContent = `${visibleNodes.length} 节点 · ${allSysServers.length} 服务器`;
    }

    // Unbound system servers: either has no node_id OR points to an ID not in visibleNodes
    const visibleNodeIds = new Set(visibleNodes.map(n => n.id));
    const unboundSysServers = allSysServers.filter(s => !s.node_id || !visibleNodeIds.has(s.node_id));

    let html = '';

    // 1. Render Hardware Nodes (with their bound servers displayed directly attached below)
    visibleNodes.forEach(node => {
      const boundServers = allSysServers.filter(s => s.node_id === node.id);
      const isChecked = state.selectedServerIds.has(node.id);
      const srvMode = node.mode || 'auto';

      html += `
        <div class="node-server-composite-wrapper" style="display:flex; flex-direction:column; gap:6px;">
          <!-- Primary Hardware Node Card (主机节点) -->
          <div class="probe-card" id="probeCard_${node.id}" data-srv-id="${node.id}">
            <div class="probe-header">
              <div class="probe-title-group" style="width:100%; display:flex; justify-content:space-between; align-items:center;">
                <div style="display:flex; align-items:flex-start; gap:8px; min-width:0; flex:1;">
                  <label class="probe-select-wrapper" onclick="event.stopPropagation();" title="选择以进行批量操作" style="margin-top:2px;">
                    <input type="checkbox" class="probe-checkbox" data-srv-id="${node.id}" ${isChecked ? 'checked' : ''} onchange="toggleServerSelection('${node.id}', this.checked)">
                  </label>
                  <span class="status-dot probe-srv-dot" style="margin-top:5px;"></span>
                  <div style="min-width:0; flex:1;">
                    <div style="display:flex; flex-direction:column; align-items:flex-start; gap:2px;">
                      <div style="display:flex; align-items:center; gap:4px;">
                        <span class="micro-capsule capsule-blue" style="font-size:8.5px; padding:0px 5px; line-height:14px;">${node.brand ? (node.brand === 'inspur' ? '浪潮 Inspur' : (node.brand === 'huawei' ? '华为 Huawei' : (node.brand === 'supermicro' ? '超微' : (node.brand === 'lenovo' ? '联想' : 'IPMI 硬件节点')))) : 'IPMI 硬件节点'}</span>
                      </div>
                      <span class="probe-name probe-srv-name" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;" title="${node.name}">${node.name}</span>
                    </div>
                    <div class="probe-model-ip probe-srv-model-ip">
                      <div style="display:flex; align-items:center; gap:6px;">
                        <span class="probe-model-name">${node.model || '通用服务器'}</span>
                      </div>
                      <span class="probe-ip-addr">BMC: ${node.ip}</span>
                      ${node.serial ? `<span class="micro-capsule capsule-indigo" style="font-size:8.5px; padding:0 4px; line-height:13px; font-weight:600; margin-top:2px; display:inline-block; align-self:flex-start;" title="出厂资产序列号 (Service Tag / SN)">SN: ${node.serial}</span>` : ''}
                    </div>
                  </div>
                </div>

                <!-- 在名字与工况指标之间这中间空出的从上到下排序的小胶囊列 -->
                <div class="probe-power-capsules-column" onclick="event.stopPropagation();" title="IPMI 机箱电源控制">
                  <button type="button" class="power-capsule capsule-pwr-on" onclick="handleChassisPower('${node.id}', 'on')" title="IPMI 远程开机 (Power On)">开机</button>
                  <button type="button" class="power-capsule capsule-pwr-soft" onclick="handleChassisPower('${node.id}', 'soft')" title="IPMI 安全软关机 (ACPI 正常关机)">关机</button>
                  <button type="button" class="power-capsule capsule-pwr-reset" onclick="handleChassisPower('${node.id}', 'reset')" title="IPMI 硬件重启 (Chassis Reset)">重启</button>
                  <button type="button" class="power-capsule capsule-pwr-off" onclick="handleChassisPower('${node.id}', 'off')" title="IPMI 强制断电 (硬关机)">断电</button>
                </div>
              </div>
            </div>

            <!-- Metrics Area with Status Pill placed ON TOP of numerical UI (不在占用整体行比) -->
            <div class="probe-metrics-box">
              <div class="probe-metrics-top-bar">
                <span class="metrics-top-title">工况指标</span>
                <span class="status-micro-pill probe-srv-status-pill offline">
                  <span class="status-dot-mini probe-srv-status-dot"></span>
                  <span class="probe-srv-status-text">检测中...</span>
                </span>
              </div>
              <div class="probe-metrics-row">
                <div class="probe-metric-item">
                  <span class="probe-metric-label">CPU 温度</span>
                  <span class="probe-metric-val probe-srv-cpu">--</span>
                  <span class="probe-metric-sub">核心最高</span>
                </div>
                <div class="probe-metric-item">
                  <span class="probe-metric-label">进气环境</span>
                  <span class="probe-metric-val probe-srv-inlet">--</span>
                  <span class="probe-metric-sub">机箱入风</span>
                </div>
                <div class="probe-metric-item">
                  <span class="probe-metric-label">风扇转速</span>
                  <span class="probe-metric-val probe-srv-speed">--</span>
                  <span class="probe-metric-sub probe-srv-rpm">-- RPM</span>
                </div>
                <div class="probe-metric-item">
                  <span class="probe-metric-label">整机功耗</span>
                  <span class="probe-metric-val probe-srv-power" style="color:var(--system-orange); font-weight:700;">--</span>
                  <span class="probe-metric-sub">实时负载</span>
                </div>
              </div>
            </div>

            <!-- 紧凑融合式温控策略选择器 (带精致微型标头，高度紧凑，零无效大片留白) -->
            <div class="probe-mode-box">
              <div class="probe-mode-top-bar">
                <span class="mode-top-title">温控策略切换</span>
                <span class="mode-top-hint">点击即时切换</span>
              </div>
              <div class="probe-mode-selector" id="probeModeSel_${node.id}">
                <button class="probe-mode-btn ${srvMode === 'dynamic' ? 'active' : ''}" onclick="setServerThermalMode('${node.id}', 'dynamic')" title="动态温控曲线：随CPU温度自适应线性调速">
                  <span class="probe-mode-dot green"></span>
                  <span>曲线</span>
                </button>
                <button class="probe-mode-btn ${srvMode === 'manual' ? 'active' : ''}" onclick="setServerThermalMode('${node.id}', 'manual')" title="手动固定转速：按设定目标恒定旋转">
                  <span class="probe-mode-dot blue"></span>
                  <span>手动</span>
                </button>
                <button class="probe-mode-btn ${srvMode === 'preset' ? 'active' : ''}" onclick="setServerThermalMode('${node.id}', 'preset')" title="情景方案：极静音/均衡/性能预设一键切换">
                  <span class="probe-mode-dot purple"></span>
                  <span>方案</span>
                </button>
                <button class="probe-mode-btn ${srvMode === 'auto' ? 'active' : ''}" onclick="setServerThermalMode('${node.id}', 'auto')" title="原厂托管：交还服务器BMC原厂动态控制">
                  <span class="probe-mode-dot gray"></span>
                  <span>原厂</span>
                </button>
              </div>
            </div>

            <div class="probe-footer">
              <div class="probe-footer-info">
                <span class="probe-footer-uptime probe-srv-uptime">在线: 检测中...</span>
                <span class="probe-footer-latency probe-srv-latency">延时: --</span>
              </div>
              <div style="display:flex; gap:6px;" class="probe-srv-actions">
                <button class="secondary-btn" style="font-size:11px; padding:2px 7px;" onclick="openHardwareNodeDetailModal('${node.id}')">详情</button>
              </div>
            </div>
          </div>

          <!-- Bound Servers Shelf (移动到主卡片外部下方独立抽屉呈现，彻底解耦工况指标对齐！) -->
          ${boundServers.length > 0 ? `
            <div class="bound-servers-shelf" id="boundShelf_${node.id}">
              ${boundServers.map(srv => renderBoundServerSubCard(srv, node.name)).join('')}
            </div>
          ` : ''}
        </div>
      `;
    });

    // 2. Render Unbound System Servers directly as standalone cards!
    if (unboundSysServers.length > 0) {
      unboundSysServers.forEach(srv => {
        html += `
          <div class="node-server-composite-wrapper" style="display:flex; flex-direction:column; gap:6px;">
            ${renderStandaloneServerCard(srv)}
          </div>
        `;
      });
    }

    container.innerHTML = html;
    updateOverviewCardTelemetry(visibleNodes, allSysServers);
    updateBatchSelectionUI();
    return;
  }

  // =========================================================================
  // VIEW MODE 2: 仅看【硬件节点】(NODES)
  // =========================================================================
  if (concept === 'nodes') {
    if (countBadge) countBadge.textContent = `${visibleNodes.length} 台硬件节点`;
    container.innerHTML = visibleNodes.map(node => {
      const isChecked = state.selectedServerIds.has(node.id);
      const srvMode = node.mode || 'auto';
      return `
        <div class="probe-card" id="probeCard_${node.id}" data-srv-id="${node.id}">
          <div class="probe-header">
            <div class="probe-title-group" style="width:100%; display:flex; justify-content:space-between; align-items:center;">
              <div style="display:flex; align-items:flex-start; gap:8px; min-width:0; flex:1;">
                <label class="probe-select-wrapper" onclick="event.stopPropagation();" title="选择以进行批量操作" style="margin-top:2px;">
                  <input type="checkbox" class="probe-checkbox" data-srv-id="${node.id}" ${isChecked ? 'checked' : ''} onchange="toggleServerSelection('${node.id}', this.checked)">
                </label>
                <span class="status-dot probe-srv-dot" style="margin-top:5px;"></span>
                <div style="min-width:0; flex:1;">
                  <div style="display:flex; flex-direction:column; align-items:flex-start; gap:2px; margin-bottom:2px;">
                    <div style="display:flex; align-items:center; gap:4px;">
                      <span class="micro-capsule capsule-blue" style="font-size:8.5px; padding:1px 5px; line-height:12px;">${node.brand ? (node.brand === 'inspur' ? '浪潮 Inspur' : (node.brand === 'huawei' ? '华为 Huawei' : (node.brand === 'supermicro' ? '超微' : (node.brand === 'lenovo' ? '联想' : 'IPMI 硬件节点')))) : 'IPMI 硬件节点'}</span>
                    </div>
                    <span class="probe-name probe-srv-name" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;" title="${node.name}">${node.name}</span>
                  </div>
                  <div class="probe-model-ip probe-srv-model-ip">
                    <div style="display:flex; align-items:center; gap:6px;">
                      <span class="probe-model-name">${node.model || '通用服务器'}</span>
                    </div>
                    <span class="probe-ip-addr">BMC: ${node.ip}</span>
                    ${node.serial ? `<span class="micro-capsule capsule-indigo" style="font-size:8.5px; padding:0 4px; line-height:13px; font-weight:600; margin-top:2px; display:inline-block; align-self:flex-start;" title="出厂资产序列号 (Service Tag / SN)">SN: ${node.serial}</span>` : ''}
                  </div>
                </div>
              </div>

              <!-- 在名字与工况指标之间这中间空出的从上到下排序的小胶囊列 -->
              <div class="probe-power-capsules-column" onclick="event.stopPropagation();" title="IPMI 机箱电源控制">
                <button type="button" class="power-capsule capsule-pwr-on" onclick="handleChassisPower('${node.id}', 'on')" title="IPMI 远程开机 (Power On)">开机</button>
                <button type="button" class="power-capsule capsule-pwr-soft" onclick="handleChassisPower('${node.id}', 'soft')" title="IPMI 安全软关机 (ACPI 正常关机)">关机</button>
                <button type="button" class="power-capsule capsule-pwr-reset" onclick="handleChassisPower('${node.id}', 'reset')" title="IPMI 硬件重启 (Chassis Reset)">重启</button>
                <button type="button" class="power-capsule capsule-pwr-off" onclick="handleChassisPower('${node.id}', 'off')" title="IPMI 强制断电 (硬关机)">断电</button>
              </div>
            </div>
          </div>

          <!-- Metrics Area with Status Pill placed ON TOP of numerical UI (不在占用整体行比) -->
          <div class="probe-metrics-box">
            <div class="probe-metrics-top-bar">
              <span class="metrics-top-title">工况指标</span>
              <span class="status-micro-pill probe-srv-status-pill offline">
                <span class="status-dot-mini probe-srv-status-dot"></span>
                <span class="probe-srv-status-text">检测中...</span>
              </span>
            </div>
            <div class="probe-metrics-row">
              <div class="probe-metric-item">
                <span class="probe-metric-label">CPU 温度</span>
                <span class="probe-metric-val probe-srv-cpu">--</span>
                <span class="probe-metric-sub">核心最高</span>
              </div>
              <div class="probe-metric-item">
                <span class="probe-metric-label">进气环境</span>
                <span class="probe-metric-val probe-srv-inlet">--</span>
                <span class="probe-metric-sub">机箱入风</span>
              </div>
              <div class="probe-metric-item">
                <span class="probe-metric-label">风扇转速</span>
                <span class="probe-metric-val probe-srv-speed">--</span>
                <span class="probe-metric-sub probe-srv-rpm">-- RPM</span>
              </div>
              <div class="probe-metric-item">
                <span class="probe-metric-label">整机功耗</span>
                <span class="probe-metric-val probe-srv-power" style="color:var(--system-orange); font-weight:700;">--</span>
                <span class="probe-metric-sub">实时负载</span>
              </div>
            </div>
          </div>

          <!-- 紧凑融合式温控策略选择器 (带精致微型标头，高度紧凑，零无效大片留白) -->
          <div class="probe-mode-box">
            <div class="probe-mode-top-bar">
              <span class="mode-top-title">温控策略切换</span>
              <span class="mode-top-hint">点击即时切换</span>
            </div>
            <div class="probe-mode-selector" id="probeModeSel_${node.id}">
              <button class="probe-mode-btn ${srvMode === 'dynamic' ? 'active' : ''}" onclick="setServerThermalMode('${node.id}', 'dynamic')" title="动态温控曲线：随CPU温度自适应线性调速">
                <span class="probe-mode-dot green"></span>
                <span>曲线</span>
              </button>
              <button class="probe-mode-btn ${srvMode === 'manual' ? 'active' : ''}" onclick="setServerThermalMode('${node.id}', 'manual')" title="手动固定转速：按设定目标恒定旋转">
                <span class="probe-mode-dot blue"></span>
                <span>手动</span>
              </button>
              <button class="probe-mode-btn ${srvMode === 'preset' ? 'active' : ''}" onclick="setServerThermalMode('${node.id}', 'preset')" title="情景方案：极静音/均衡/性能预设一键切换">
                <span class="probe-mode-dot purple"></span>
                <span>方案</span>
              </button>
              <button class="probe-mode-btn ${srvMode === 'auto' ? 'active' : ''}" onclick="setServerThermalMode('${node.id}', 'auto')" title="原厂托管：交还服务器BMC原厂动态控制">
                <span class="probe-mode-dot gray"></span>
                <span>原厂</span>
              </button>
            </div>
          </div>

          <div class="probe-footer">
            <div class="probe-footer-info">
              <span class="probe-footer-uptime probe-srv-uptime">在线: 检测中...</span>
              <span class="probe-footer-latency probe-srv-latency">延时: --</span>
            </div>
            <div style="display:flex; gap:6px;" class="probe-srv-actions">
              <button class="secondary-btn" style="font-size:11px; padding:2px 7px;" onclick="openHardwareNodeDetailModal('${node.id}')">详情</button>
            </div>
          </div>
        </div>
      `;
    }).join('');

    updateOverviewCardTelemetry(visibleNodes, []);
    updateBatchSelectionUI();
    return;
  }

  // =========================================================================
  // VIEW MODE 3: 仅看【系统服务器】(SERVERS)
  // =========================================================================
  if (concept === 'servers') {
    if (countBadge) countBadge.textContent = `${allSysServers.length} 台系统服务器`;
    if (allSysServers.length === 0) {
      container.innerHTML = `
        <div style="grid-column: 1 / -1; padding: 40px; text-align: center; color: var(--text-tertiary);">
          <div style="font-size: 14px; font-weight: 500; margin-bottom: 6px;">暂未配置系统服务器</div>
          <div style="font-size: 12px;">请前往「节点与服务器管理」点击「+ 添加系统服务器」</div>
        </div>
      `;
      return;
    }
    container.innerHTML = allSysServers.map(srv => renderStandaloneServerCard(srv)).join('');
    updateOverviewCardTelemetry([], allSysServers);
  }
}

function renderBoundServerSubCard(srv, parentNodeName) {
  const isConn = Boolean(srv && srv.connected === true);
  const isConnecting = srv ? (srv.connected === null || srv.connected === undefined) : false;
  const latency = isConn ? (srv.latency_ms ? `${srv.latency_ms}ms` : '<10ms') : (isConnecting ? '连接中' : '断开');
  const placeholder = isConnecting ? '获取中' : '--';
  const cpuPct = isConn && srv.cpu_pct !== undefined ? srv.cpu_pct : 0;
  const memPct = isConn && srv.mem_pct !== undefined ? srv.mem_pct : 0;
  const swapPct = isConn && srv.swap_pct !== undefined ? srv.swap_pct : 0;
  const diskPct = isConn && srv.disk_pct !== undefined ? srv.disk_pct : 0;

  // 优雅分行架构：第一行名字+IP端点+延时+详情，第二行工况指标胶囊；长条模式下则自动一行贯通对齐
  return `
    <div class="bound-subcard-strip" 
         id="boundSub_${srv.id}"
         data-bound-srv-id="${srv.id}"
         onclick="openServerDetailModal('${srv.id}')"
         title="点击查看「${srv.name}」系统底层运行详情 (${isConn ? `在线 · 延时: ${latency}` : (isConnecting ? '正在获取遥测...' : (srv.last_error || '离线未连'))})">
      
      <!-- Row 1: Left: Indicator, compact name, IP endpoint; Right: Latency & Detail button -->
      <div class="bound-strip-top-row">
        <div class="bound-strip-left">
          <span class="bound-strip-arrow">↳</span>
          <span class="status-dot bound-srv-dot" style="background:${isConn ? 'var(--system-green)' : (isConnecting ? 'var(--system-blue)' : 'var(--system-orange)')}; width:6px; height:6px; flex-shrink:0;"></span>
          <span class="bound-strip-name" title="${srv.name}">${srv.name}</span>
          <span class="bound-strip-host-ip" title="${srv.host}:${srv.port || 22}">${srv.host}</span>
          <span class="micro-capsule ${isConn ? 'capsule-blue' : (isConnecting ? 'capsule-blue' : 'capsule-orange')}" style="font-size:8px; padding:0 3px; line-height:11px;">系统</span>
        </div>
        <div class="bound-strip-right bound-strip-compact-right">
          <span class="bound-strip-latency bound-srv-latency" title="网络延时">${latency}</span>
          <span class="bound-strip-btn">详情</span>
        </div>
      </div>

      <!-- Row 2: Auto-scaling Zabbix/Probe capsules with internal fill gauge (信息一行，不重叠) -->
      <div class="probe-capsule-container">
        <!-- CPU Capsule -->
        <div class="probe-capsule capsule-cpu filled-gauge-capsule" 
             style="background: ${isConn ? `linear-gradient(to right, ${cpuPct > 80 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(10, 132, 255, 0.22)'} ${cpuPct}%, var(--surface-secondary) ${cpuPct}%)` : 'var(--surface-secondary)'}; border-color: ${isConn ? (cpuPct > 80 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(10, 132, 255, 0.3)') : 'var(--border-subtle)'};"
             title="CPU 算力负载: ${isConn ? `${cpuPct}%` : (isConnecting ? '正在探测...' : '离线')}">
          <span class="probe-capsule-tag" style="color:var(--accent-blue); font-weight:700;">CPU</span>
          <span class="probe-capsule-val">${isConn ? `${cpuPct}%` : placeholder}</span>
        </div>

        <!-- RAM Capsule -->
        <div class="probe-capsule capsule-ram filled-gauge-capsule" 
             style="background: ${isConn ? `linear-gradient(to right, ${memPct > 85 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(52, 199, 89, 0.22)'} ${memPct}%, var(--surface-secondary) ${memPct}%)` : 'var(--surface-secondary)'}; border-color: ${isConn ? (memPct > 85 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(52, 199, 89, 0.3)') : 'var(--border-subtle)'};"
             title="物理内存水线: ${isConn ? `${memPct}%` : (isConnecting ? '正在探测...' : '离线')}">
          <span class="probe-capsule-tag" style="color:var(--system-green); font-weight:700;">RAM</span>
          <span class="probe-capsule-val">${isConn ? `${memPct}%` : placeholder}</span>
        </div>

        <!-- Swap Capsule -->
        <div class="probe-capsule capsule-swap filled-gauge-capsule" 
             style="background: ${isConn ? `linear-gradient(to right, ${swapPct > 50 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(255, 149, 0, 0.22)'} ${swapPct}%, var(--surface-secondary) ${swapPct}%)` : 'var(--surface-secondary)'}; border-color: ${isConn ? (swapPct > 50 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(255, 149, 0, 0.3)') : 'var(--border-subtle)'};"
             title="Swap 换页占用: ${isConn ? `${swapPct}%` : (isConnecting ? '正在探测...' : '离线')}">
          <span class="probe-capsule-tag" style="color:var(--system-orange); font-weight:700;">SWP</span>
          <span class="probe-capsule-val">${isConn ? `${swapPct}%` : placeholder}</span>
        </div>

        <!-- Disk Capsule -->
        <div class="probe-capsule capsule-disk filled-gauge-capsule" 
             style="background: ${isConn ? `linear-gradient(to right, ${diskPct > 85 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(175, 82, 222, 0.22)'} ${diskPct}%, var(--surface-secondary) ${diskPct}%)` : 'var(--surface-secondary)'}; border-color: ${isConn ? (diskPct > 85 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(175, 82, 222, 0.3)') : 'var(--border-subtle)'};"
             title="存储空间占用: ${isConn ? `${diskPct}%` : (isConnecting ? '正在探测...' : '离线')}">
          <span class="probe-capsule-tag" style="color:var(--accent-purple); font-weight:700;">DSK</span>
          <span class="probe-capsule-val">${isConn ? `${diskPct}%` : placeholder}</span>
        </div>
      </div>

      <!-- Row-Mode Trailing Details Action (对齐长条模式下父节点卡片最右侧操作栏) -->
      <div class="bound-strip-right bound-strip-row-right">
        <span class="bound-strip-latency bound-srv-latency" title="网络延时">${latency}</span>
        <span class="bound-strip-btn">详情</span>
      </div>
    </div>
  `;
}

function renderStandaloneServerCard(srv) {
  const isConn = Boolean(srv && srv.connected === true);
  const isConnecting = srv ? (srv.connected === null || srv.connected === undefined) : false;
  const boundNode = state.servers.find(n => n.id === srv.node_id);
  const boundNodeName = boundNode ? boundNode.name : '独立服务器';
  const latency = isConn ? (srv.latency_ms ? `${srv.latency_ms} ms` : '<10 ms') : (isConnecting ? '连接中' : '离线');
  const placeholder = isConnecting ? '获取中' : '--';
  const cpuPct = isConn && srv.cpu_pct !== undefined ? srv.cpu_pct : 0;
  const memPct = isConn && srv.mem_pct !== undefined ? srv.mem_pct : 0;
  const swapPct = isConn && srv.swap_pct !== undefined ? srv.swap_pct : 0;
  const diskPct = isConn && srv.disk_pct !== undefined ? srv.disk_pct : 0;
  const cores = srv.cpu_cores || 8;
  const memTotal = srv.mem_total_gb || 32.0;
  const memUsed = srv.mem_used_gb || (Math.round((memTotal * memPct / 100) * 10) / 10);
  const diskTotal = srv.disk_total_gb || 512.0;
  const diskUsed = srv.disk_used_gb || (Math.round((diskTotal * diskPct / 100) * 10) / 10);
  const swapTotal = srv.swap_total_gb || 8.0;
  const swapUsed = srv.swap_used_gb || (Math.round((swapTotal * swapPct / 100) * 10) / 10);

  const uptimeSec = srv.uptime_sec || 0;
  const uptimeDays = Math.floor(uptimeSec / 86400);
  const uptimeHours = Math.floor((uptimeSec % 86400) / 3600);
  const uptimeMins = Math.floor((uptimeSec % 3600) / 60);
  let uptimeText = isConn ? (uptimeDays > 0 ? `在线: ${uptimeDays} 天 ${uptimeHours} 小时` : (uptimeHours > 0 ? `在线: ${uptimeHours} 小时 ${uptimeMins} 分` : `在线: ${uptimeMins} 分钟`)) : (isConnecting ? '正在获取指标...' : '离线未连');

  const isChecked = state.selectedServerIds.has(srv.id);

  // 完美适应硬件节点卡片的架构与视觉语言：名称对齐、一行两个两行并行、在线时间在上延时在下
  return `
    <div class="probe-card standalone-server-card" id="sysCard_${srv.id}" data-srv-id="${srv.id}">
      <!-- Header: exactly matching hardware node card layout, checkbox & capsule on top of name -->
      <div class="probe-header">
        <div class="probe-title-group" style="width:100%;">
          <label class="probe-select-wrapper" onclick="event.stopPropagation();" title="选择以进行批量操作">
            <input type="checkbox" class="probe-checkbox" data-srv-id="${srv.id}" ${isChecked ? 'checked' : ''} onchange="toggleServerSelection('${srv.id}', this.checked)">
          </label>
          <span class="status-dot" style="background:${isConn ? 'var(--system-green)' : (isConnecting ? 'var(--system-blue)' : 'var(--system-orange)')};"></span>
          <div style="min-width:0; flex:1;">
            <div style="display:flex; flex-direction:column; align-items:flex-start; gap:2px; margin-bottom:2px;">
              <span class="micro-capsule ${boundNode ? 'capsule-indigo' : 'capsule-cyan'}" style="font-size:8.5px; padding:1px 5px; line-height:12px;">
                ${boundNode ? '已绑系统服务器' : '独立系统服务器'}
              </span>
              <span class="probe-name" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;" title="${srv.name}">${srv.name}</span>
            </div>
            <div class="probe-model-ip">
              <span class="probe-model-name">${srv.os_name || 'Linux OS'}</span>
              <span class="probe-ip-addr">SSH: ${srv.host}:${srv.port || 22}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Metrics Area with Status Pill placed ON TOP of numerical UI (不在占用整体行比) -->
      <div class="probe-metrics-box">
        <div class="probe-metrics-top-bar">
          <span class="metrics-top-title" style="white-space:nowrap; flex-shrink:0;">系统指标</span>
          <span class="status-micro-pill sys-srv-status-pill ${isConn ? 'online' : (isConnecting ? 'connecting' : 'offline')}">
            <span class="status-dot-mini sys-srv-status-dot" style="background:${isConn ? 'var(--system-green)' : (isConnecting ? 'var(--system-blue)' : 'var(--system-orange)')};"></span>
            <span class="sys-srv-status-text">${isConn ? (state.demo_mode ? '在线 (仿真)' : '在线') : (isConnecting ? '正在获取' : '离线')}</span>
          </span>
        </div>
        <div class="probe-metrics-row">
          <!-- 1. CPU 负载 -->
          <div class="probe-metric-item">
            <span class="probe-metric-label">CPU 负载</span>
            <span class="probe-metric-val sys-srv-cpu" style="color:var(--accent-blue); font-size:14px; margin-top:1px;">${isConn ? `${cpuPct}%` : placeholder}</span>
            <span class="probe-metric-sub sys-srv-cores" title="${cores} 核心">${isConn ? `${cores} 核心` : (isConnecting ? '探测中' : '--')}</span>
          </div>

          <!-- 2. 物理内存 -->
          <div class="probe-metric-item">
            <span class="probe-metric-label">物理内存</span>
            <span class="probe-metric-val sys-srv-mem" style="color:var(--system-green); font-size:14px; margin-top:1px;">${isConn ? `${memPct}%` : placeholder}</span>
            <span class="probe-metric-sub sys-srv-mem-sub" title="${memUsed}G / ${memTotal}G">${isConn ? `${memUsed}G / ${memTotal}G` : (isConnecting ? '探测中' : '--')}</span>
          </div>

          <!-- 3. 根盘空间 -->
          <div class="probe-metric-item">
            <span class="probe-metric-label">根盘空间</span>
            <span class="probe-metric-val sys-srv-disk" style="color:var(--system-purple); font-size:14px; margin-top:1px;">${isConn ? `${diskPct}%` : placeholder}</span>
            <span class="probe-metric-sub sys-srv-disk-sub" title="${diskUsed}G / ${diskTotal}G">${isConn ? `${diskUsed}G / ${diskTotal}G` : (isConnecting ? '探测中' : '--')}</span>
          </div>

          <!-- 4. 网络延时 -->
          <div class="probe-metric-item">
            <span class="probe-metric-label">网络延时</span>
            <span class="probe-metric-val sys-srv-latency-val" style="font-size:14px; margin-top:1px;">${latency}</span>
            <span class="probe-metric-sub">SSH 响应</span>
          </div>
        </div>
      </div>

      <!-- Middle bar: 2x2 grid (一行两个，两行并行，胶囊内部依据百分比进度占色填充，无需额外进度条) -->
      <div class="server-capsule-bar" onclick="openServerDetailModal('${srv.id}')" title="点击查看「${srv.name}」系统运行详情与探针全量指标" style="cursor:pointer;">
        <!-- CPU Capsule: 渐变色百分比内部填充背景 -->
        <div class="probe-capsule capsule-cpu filled-gauge-capsule" style="background: ${isConn ? `linear-gradient(to right, ${cpuPct > 80 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(10, 132, 255, 0.22)'} ${cpuPct}%, var(--surface-secondary) ${cpuPct}%)` : 'var(--surface-secondary)'}; border-color: ${isConn ? (cpuPct > 80 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(10, 132, 255, 0.3)') : 'var(--border-subtle)'};" title="CPU负载: ${isConn ? `${cpuPct}% (${cores}核)` : (isConnecting ? '正在探测...' : '离线')}">
          <span class="probe-capsule-tag" style="color:var(--accent-blue); font-weight:700;">CPU</span>
          <span class="probe-capsule-val">${isConn ? `${cpuPct}%` : placeholder}</span>
        </div>
        <!-- RAM Capsule -->
        <div class="probe-capsule capsule-ram filled-gauge-capsule" style="background: ${isConn ? `linear-gradient(to right, ${memPct > 85 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(52, 199, 89, 0.22)'} ${memPct}%, var(--surface-secondary) ${memPct}%)` : 'var(--surface-secondary)'}; border-color: ${isConn ? (memPct > 85 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(52, 199, 89, 0.3)') : 'var(--border-subtle)'};" title="内存占用: ${isConn ? `${memUsed}G / ${memTotal}G (${memPct}%)` : (isConnecting ? '正在探测...' : '离线')}">
          <span class="probe-capsule-tag" style="color:var(--system-green); font-weight:700;">内存</span>
          <span class="probe-capsule-val">${isConn ? `${memPct}%` : placeholder}</span>
        </div>
        <!-- SWP Capsule -->
        <div class="probe-capsule capsule-swap filled-gauge-capsule" style="background: ${isConn ? `linear-gradient(to right, ${swapPct > 50 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(255, 149, 0, 0.22)'} ${swapPct}%, var(--surface-secondary) ${swapPct}%)` : 'var(--surface-secondary)'}; border-color: ${isConn ? (swapPct > 50 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(255, 149, 0, 0.3)') : 'var(--border-subtle)'};" title="Swap换页: ${isConn ? `${swapUsed}G / ${swapTotal}G (${swapPct}%)` : (isConnecting ? '正在探测...' : '离线')}">
          <span class="probe-capsule-tag" style="color:var(--system-orange); font-weight:700;">SWP</span>
          <span class="probe-capsule-val">${isConn ? `${swapPct}%` : placeholder}</span>
        </div>
        <!-- DISK Capsule -->
        <div class="probe-capsule capsule-disk filled-gauge-capsule" style="background: ${isConn ? `linear-gradient(to right, ${diskPct > 85 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(175, 82, 222, 0.22)'} ${diskPct}%, var(--surface-secondary) ${diskPct}%)` : 'var(--surface-secondary)'}; border-color: ${isConn ? (diskPct > 85 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(175, 82, 222, 0.3)') : 'var(--border-subtle)'};" title="存储空间: ${isConn ? `${diskUsed}G / ${diskTotal}G (${diskPct}%)` : (isConnecting ? '正在探测...' : '离线')}">
          <span class="probe-capsule-tag" style="color:var(--system-purple); font-weight:700;">存储</span>
          <span class="probe-capsule-val">${isConn ? `${diskPct}%` : placeholder}</span>
        </div>
      </div>

      <!-- Footer: 在线时间在上，延时在下，取消负载，充分合理利用空间 -->
      <div class="probe-footer">
        <div class="probe-footer-info">
          <span class="probe-footer-uptime sys-srv-uptime">${uptimeText}</span>
          <span class="probe-footer-latency sys-srv-latency">延时: ${latency}</span>
        </div>
        <div class="probe-srv-actions" style="display:flex; gap:6px;">
          <button class="secondary-btn" style="font-size:11px; padding:2px 8px; border-radius:9999px;" onclick="openServerDetailModal('${srv.id}')">详情</button>
        </div>
      </div>
    </div>
  `;
}

function updateOverviewCardTelemetry(nodes, servers) {
  // 实时汇总计算集群所有在线硬件节点的实时功耗总和
  let clusterWattsSum = 0;
  let onlineNodesWithPower = 0;

  // 1. Hardware Nodes
  nodes.forEach(srv => {
    const card = document.getElementById(`probeCard_${srv.id}`);

    const telemetry = state.cluster_telemetry.find(t => t.id === srv.id) || {};
    const isConn = telemetry.connected === true;
    const isConnecting = telemetry.connected === null || telemetry.connected === undefined;

    // 统计在线节点功耗
    const pInfo = telemetry.power || (state.active_server && state.active_server.id === srv.id ? state.power : null);
    if (isConn && pInfo && pInfo.total_watts !== null && pInfo.total_watts !== undefined) {
      clusterWattsSum += Number(pInfo.total_watts) || 0;
      onlineNodesWithPower++;
    }

    if (!card) return;

    // 数据展示要求：
    // 1. 如果节点在线 (isConn)，但某些轻量数据（CPU温度、进气、转速、功耗等）正在采样尚未返回时，显示精致微型的「获取中...」
    // 2. 如果节点处于探测连接中 (isConnecting)，所有指标显示精致微型的「获取中...」
    // 3. 只有当节点最终正式确认掉线 (isConn === false && !isConnecting)，才绝对不显示任何具体数据，全部清空为 '--'
    const isOffline = !isConn && !isConnecting;
    const placeholder = isOffline ? '--' : '<span class="loading-placeholder">获取中...</span>';

    const maxCpu = (isConn && telemetry.max_cpu_temp !== null && telemetry.max_cpu_temp !== undefined) ? `${telemetry.max_cpu_temp} °C` : placeholder;
    const inlet = (isConn && telemetry.inlet_temp !== null && telemetry.inlet_temp !== undefined) ? `${telemetry.inlet_temp} °C` : placeholder;
    const fanSpeed = (isConn && telemetry.fan_target_pct !== null && telemetry.fan_target_pct !== undefined) ? `${telemetry.fan_target_pct}%` : placeholder;
    const avgRpmStr = (isConn && telemetry.avg_fan_rpm) ? `${telemetry.avg_fan_rpm} RPM` : (isOffline ? '--' : '<span class="loading-placeholder">获取中...</span>');
    const latency = isConn ? `${telemetry.latency_ms || 10} ms` : (isConnecting ? '连接中' : '离线');

    const uptimeSec = telemetry.uptime_sec || 0;
    const uDays = Math.floor(uptimeSec / 86400);
    const uHours = Math.floor((uptimeSec % 86400) / 3600);
    const uMins = Math.floor((uptimeSec % 3600) / 60);
    let uptimeStr = isConn ? (uDays > 0 ? `在线: ${uDays} 天 ${uHours} 小时` : (uHours > 0 ? `在线: ${uHours} 小时 ${uMins} 分` : `在线: ${uMins} 分钟`)) : (isOffline ? '离线未连' : '正在获取连接...');

    const dot = card.querySelector('.probe-srv-dot');
    if (dot) dot.style.background = isConn ? 'var(--system-green)' : (isConnecting ? 'var(--system-blue)' : 'var(--system-red)');

    const pill = card.querySelector('.probe-srv-status-pill');
    const pillDot = card.querySelector('.probe-srv-status-dot');
    const pillText = card.querySelector('.probe-srv-status-text');
    if (pill) {
      pill.className = `status-micro-pill probe-srv-status-pill ${isConn ? 'online' : (isConnecting ? 'connecting' : 'offline')}`;
      if (pillDot) pillDot.style.background = isConn ? 'var(--system-green)' : (isConnecting ? 'var(--system-blue)' : 'var(--system-red)');
      if (pillText) pillText.textContent = isConn ? (state.demo_mode ? '在线 (仿真)' : '在线') : (isConnecting ? '正在获取' : '离线');
      if (!isConn && telemetry.error_msg) {
        pill.title = isConnecting ? '正在向 BMC 发送握手与数据探测报文...' : `离线原因: ${telemetry.error_msg}`;
      }
    }

    const cpuEl = card.querySelector('.probe-srv-cpu');
    if (cpuEl) cpuEl.innerHTML = maxCpu;

    const inletEl = card.querySelector('.probe-srv-inlet');
    if (inletEl) inletEl.innerHTML = inlet;

    const speedEl = card.querySelector('.probe-srv-speed');
    if (speedEl) speedEl.innerHTML = fanSpeed;

    const rpmEl = card.querySelector('.probe-srv-rpm');
    if (rpmEl) rpmEl.innerHTML = avgRpmStr;

    const powerInfo = telemetry.power || (state.active_server && state.active_server.id === srv.id ? state.power : null);
    const totalWatts = (isConn && powerInfo && powerInfo.total_watts !== null && powerInfo.total_watts !== undefined) ? `${powerInfo.total_watts} W` : placeholder;
    const powerEl = card.querySelector('.probe-srv-power');
    if (powerEl) {
      powerEl.innerHTML = totalWatts;
      if (powerInfo && (powerInfo.ps1 || powerInfo.ps2)) {
        const ps1w = powerInfo.ps1 && powerInfo.ps1.watts !== null ? `${powerInfo.ps1.watts}W` : '离线';
        const ps2w = powerInfo.ps2 && powerInfo.ps2.watts !== null ? `${powerInfo.ps2.watts}W` : '离线';
        powerEl.title = `⚡ 双电源供电实况:\n• 电源 1 (PS1): ${ps1w} (${powerInfo.ps1 && powerInfo.ps1.online ? '在线负载' : '未接/待机'})\n• 电源 2 (PS2): ${ps2w} (${powerInfo.ps2 && powerInfo.ps2.online ? '在线冗余' : '未接/待机'})`;
      }
    }

    const uptimeEl = card.querySelector('.probe-srv-uptime');
    if (uptimeEl) uptimeEl.textContent = uptimeStr;

    const latencyEl = card.querySelector('.probe-srv-latency');
    if (latencyEl) latencyEl.textContent = `延时: ${latency}`;

    const actionsContainer = card.querySelector('.probe-srv-actions');
    if (actionsContainer) {
      actionsContainer.innerHTML = `
        <button class="secondary-btn" style="font-size:11px; padding:2px 7px;" onclick="openHardwareNodeDetailModal('${srv.id}')">详情</button>
      `;
    }
  });

  // 2. Standalone System Servers
  (servers || []).forEach(srv => {
    const card = document.getElementById(`sysCard_${srv.id}`);
    if (!card) return;

    const isConn = Boolean(srv && srv.connected === true);
    const isConnecting = srv ? (srv.connected === null || srv.connected === undefined) : false;
    const latency = isConn ? (srv.latency_ms ? `${srv.latency_ms} ms` : '<10 ms') : (isConnecting ? '连接中' : '离线');
    const placeholder = isConnecting ? '获取中...' : '--';
    const cpuPct = isConn && srv.cpu_pct !== undefined ? srv.cpu_pct : null;
    const memPct = isConn && srv.mem_pct !== undefined ? srv.mem_pct : null;
    const diskPct = isConn && srv.disk_pct !== undefined ? srv.disk_pct : null;
    const swapPct = isConn && srv.swap_pct !== undefined ? srv.swap_pct : null;
    const cores = srv.cpu_cores || 8;
    const memTotal = srv.mem_total_gb ? srv.mem_total_gb.toFixed(1) : '32.0';
    const memUsed = srv.mem_used_gb ? srv.mem_used_gb.toFixed(1) : '8.5';
    const swapTotal = srv.swap_total_gb ? srv.swap_total_gb.toFixed(1) : '8.0';
    const swapUsed = srv.swap_used_gb ? srv.swap_used_gb.toFixed(1) : '0.0';
    const diskTotal = srv.disk_total_gb ? srv.disk_total_gb.toFixed(0) : '500';
    const diskUsed = srv.disk_used_gb ? srv.disk_used_gb.toFixed(0) : '120';

    const uptimeSec = srv.uptime_sec || 0;
    const uDays = Math.floor(uptimeSec / 86400);
    const uHours = Math.floor((uptimeSec % 86400) / 3600);
    const uMins = Math.floor((uptimeSec % 3600) / 60);
    let uptimeStr = isConn ? (uDays > 0 ? `在线: ${uDays} 天 ${uHours} 小时` : (uHours > 0 ? `在线: ${uHours} 小时 ${uMins} 分` : `在线: ${uMins} 分钟`)) : (isConnecting ? '正在获取指标...' : '离线未连');

    const pill = card.querySelector('.sys-srv-status-pill');
    const pillDot = card.querySelector('.sys-srv-status-dot');
    const pillText = card.querySelector('.sys-srv-status-text');
    if (pill) {
      pill.className = `status-micro-pill sys-srv-status-pill ${isConn ? 'online' : (isConnecting ? 'connecting' : 'offline')}`;
      if (pillDot) pillDot.style.background = isConn ? 'var(--system-green)' : (isConnecting ? 'var(--system-blue)' : 'var(--system-orange)');
      if (pillText) pillText.textContent = isConn ? (state.demo_mode ? '在线 (仿真)' : '在线') : (isConnecting ? '正在获取' : '离线');
    }

    const cpuEl = card.querySelector('.sys-srv-cpu');
    if (cpuEl) cpuEl.textContent = isConn ? `${cpuPct}%` : placeholder;
    const coresEl = card.querySelector('.sys-srv-cores');
    if (coresEl) {
      coresEl.textContent = isConn ? `${cores} 核心` : (isConnecting ? '探测核心中' : '--');
      coresEl.title = isConn ? `${cores} 核心` : '';
    }

    const memEl = card.querySelector('.sys-srv-mem');
    if (memEl) memEl.textContent = isConn ? `${memPct}%` : placeholder;
    const memSubEl = card.querySelector('.sys-srv-mem-sub');
    if (memSubEl) {
      memSubEl.textContent = isConn ? `${memUsed}G / ${memTotal}G` : (isConnecting ? '探测内存中' : '--');
      memSubEl.title = isConn ? `${memUsed}G / ${memTotal}G` : '';
    }

    const diskEl = card.querySelector('.sys-srv-disk');
    if (diskEl) diskEl.textContent = isConn ? `${diskPct}%` : placeholder;
    const diskSubEl = card.querySelector('.sys-srv-disk-sub');
    if (diskSubEl) {
      diskSubEl.textContent = isConn ? `${diskUsed}G / ${diskTotal}G` : (isConnecting ? '探测磁盘中' : '--');
      diskSubEl.title = isConn ? `${diskUsed}G / ${diskTotal}G` : '';
    }

    // 2x2 Middle Bar update: 精炼百分比呈现，绝不溢出或被遮挡
    const midCpu = card.querySelector('.capsule-cpu');
    const midCpuVal = card.querySelector('.capsule-cpu .probe-capsule-val');
    if (midCpuVal) midCpuVal.textContent = isConn ? `${cpuPct}%` : placeholder;
    if (midCpu) {
      midCpu.title = isConn ? `CPU负载: ${cpuPct}% (${cores}核)` : (isConnecting ? '正在探测 CPU...' : '离线');
      midCpu.style.background = isConn
        ? `linear-gradient(to right, ${cpuPct > 80 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(10, 132, 255, 0.22)'} ${cpuPct}%, var(--surface-secondary) ${cpuPct}%)`
        : 'var(--surface-secondary)';
      midCpu.style.borderColor = isConn
        ? (cpuPct > 80 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(10, 132, 255, 0.3)')
        : 'var(--border-subtle)';
    }

    const midMem = card.querySelector('.capsule-ram');
    const midMemVal = card.querySelector('.capsule-ram .probe-capsule-val');
    if (midMemVal) midMemVal.textContent = isConn ? `${memPct}%` : placeholder;
    if (midMem) {
      midMem.title = isConn ? `内存占用: ${memUsed}G / ${memTotal}G (${memPct}%)` : (isConnecting ? '正在探测内存...' : '离线');
      midMem.style.background = isConn
        ? `linear-gradient(to right, ${memPct > 85 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(52, 199, 89, 0.22)'} ${memPct}%, var(--surface-secondary) ${memPct}%)`
        : 'var(--surface-secondary)';
      midMem.style.borderColor = isConn
        ? (memPct > 85 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(52, 199, 89, 0.3)')
        : 'var(--border-subtle)';
    }

    const midSwap = card.querySelector('.capsule-swap');
    const midSwapVal = card.querySelector('.capsule-swap .probe-capsule-val');
    if (midSwapVal) midSwapVal.textContent = isConn ? `${swapPct}%` : placeholder;
    if (midSwap) {
      midSwap.title = isConn ? `Swap换页: ${swapUsed}G / ${swapTotal}G (${swapPct}%)` : (isConnecting ? '正在探测 Swap...' : '离线');
      midSwap.style.background = isConn
        ? `linear-gradient(to right, ${swapPct > 50 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(255, 149, 0, 0.22)'} ${swapPct}%, var(--surface-secondary) ${swapPct}%)`
        : 'var(--surface-secondary)';
      midSwap.style.borderColor = isConn
        ? (swapPct > 50 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(255, 149, 0, 0.3)')
        : 'var(--border-subtle)';
    }

    const midDisk = card.querySelector('.capsule-disk');
    const midDiskVal = card.querySelector('.capsule-disk .probe-capsule-val');
    if (midDiskVal) midDiskVal.textContent = isConn ? `${diskPct}%` : placeholder;
    if (midDisk) {
      midDisk.title = isConn ? `存储空间: ${diskUsed}G / ${diskTotal}G (${diskPct}%)` : (isConnecting ? '正在探测磁盘...' : '离线');
      midDisk.style.background = isConn
        ? `linear-gradient(to right, ${diskPct > 85 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(175, 82, 222, 0.22)'} ${diskPct}%, var(--surface-secondary) ${diskPct}%)`
        : 'var(--surface-secondary)';
      midDisk.style.borderColor = isConn
        ? (diskPct > 85 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(175, 82, 222, 0.3)')
        : 'var(--border-subtle)';
    }

    const uptimeEl = card.querySelector('.sys-srv-uptime');
    if (uptimeEl) uptimeEl.textContent = uptimeStr;

    const latEl = card.querySelector('.sys-srv-latency');
    if (latEl) latEl.textContent = `延时: ${latency}`;
  });

  // 3. Bound Subcard Strips (全面支持总览卡片下挂载的子服务器实时平滑更新)
  (servers || []).forEach(srv => {
    const strip = document.getElementById(`boundSub_${srv.id}`);
    if (!strip) return;

    const isConn = Boolean(srv && srv.connected === true);
    const latency = isConn ? (srv.latency_ms ? `${srv.latency_ms}ms` : '<10ms') : '断开';
    const cpuPct = isConn && srv.cpu_pct !== undefined ? srv.cpu_pct : 0;
    const memPct = isConn && srv.mem_pct !== undefined ? srv.mem_pct : 0;
    const diskPct = isConn && srv.disk_pct !== undefined ? srv.disk_pct : 0;
    const swapPct = isConn && srv.swap_pct !== undefined ? srv.swap_pct : 0;

    const dot = strip.querySelector('.bound-srv-dot');
    if (dot) dot.style.background = isConn ? 'var(--system-green)' : 'var(--system-orange)';

    const latEls = strip.querySelectorAll('.bound-srv-latency');
    latEls.forEach(el => { el.textContent = latency; });

    // CPU Capsule
    const cpuCap = strip.querySelector('.capsule-cpu');
    const cpuVal = strip.querySelector('.capsule-cpu .probe-capsule-val');
    if (cpuVal) cpuVal.textContent = isConn ? `${cpuPct}%` : '--';
    if (cpuCap) {
      cpuCap.style.background = isConn
        ? `linear-gradient(to right, ${cpuPct > 80 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(10, 132, 255, 0.22)'} ${cpuPct}%, var(--surface-secondary) ${cpuPct}%)`
        : 'var(--surface-secondary)';
      cpuCap.style.borderColor = isConn
        ? (cpuPct > 80 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(10, 132, 255, 0.3)')
        : 'var(--border-subtle)';
    }

    // RAM Capsule
    const memCap = strip.querySelector('.capsule-ram');
    const memVal = strip.querySelector('.capsule-ram .probe-capsule-val');
    if (memVal) memVal.textContent = isConn ? `${memPct}%` : '--';
    if (memCap) {
      memCap.style.background = isConn
        ? `linear-gradient(to right, ${memPct > 85 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(52, 199, 89, 0.22)'} ${memPct}%, var(--surface-secondary) ${memPct}%)`
        : 'var(--surface-secondary)';
      memCap.style.borderColor = isConn
        ? (memPct > 85 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(52, 199, 89, 0.3)')
        : 'var(--border-subtle)';
    }

    // Swap Capsule
    const swpCap = strip.querySelector('.capsule-swap');
    const swpVal = strip.querySelector('.capsule-swap .probe-capsule-val');
    if (swpVal) swpVal.textContent = isConn ? `${swapPct}%` : '--';
    if (swpCap) {
      swpCap.style.background = isConn
        ? `linear-gradient(to right, ${swapPct > 50 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(255, 149, 0, 0.22)'} ${swapPct}%, var(--surface-secondary) ${swapPct}%)`
        : 'var(--surface-secondary)';
      swpCap.style.borderColor = isConn
        ? (swapPct > 50 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(255, 149, 0, 0.3)')
        : 'var(--border-subtle)';
    }

    // Disk Capsule
    const diskCap = strip.querySelector('.capsule-disk');
    const diskVal = strip.querySelector('.capsule-disk .probe-capsule-val');
    if (diskVal) diskVal.textContent = isConn ? `${diskPct}%` : '--';
    if (diskCap) {
      diskCap.style.background = isConn
        ? `linear-gradient(to right, ${diskPct > 85 ? 'rgba(255, 69, 58, 0.28)' : 'rgba(175, 82, 222, 0.22)'} ${diskPct}%, var(--surface-secondary) ${diskPct}%)`
        : 'var(--surface-secondary)';
      diskCap.style.borderColor = isConn
        ? (diskPct > 85 ? 'rgba(255, 69, 58, 0.4)' : 'rgba(175, 82, 222, 0.3)')
        : 'var(--border-subtle)';
    }
  });

  // 更新首页集群总功耗动态徽章 (所有在线节点功耗相加)
  const clusterTotalPowerEl = document.getElementById('clusterTotalPowerVal');
  if (clusterTotalPowerEl) {
    if (onlineNodesWithPower > 0) {
      clusterTotalPowerEl.textContent = `${Math.round(clusterWattsSum * 10) / 10} W`;
    } else {
      clusterTotalPowerEl.textContent = '-- W';
    }
  }
}

// ==========================================
// System Server Detail Modal Interactive Logic
// ==========================================
window.openServerDetailModal = function(sysId) {
  const srv = state.system_servers.find(s => s.id === sysId);
  if (!srv) {
    showToast('未找到对应的系统服务器数据', 'warning');
    return;
  }

  const modal = document.getElementById('serverDetailModalBackdrop');
  if (!modal) return;

  const boundNode = state.servers.find(n => n.id === srv.node_id);
  const boundNodeName = boundNode ? `${boundNode.name} (${boundNode.ip})` : '独立服务器';

  const cores = srv.cpu_cores || 16;
  const memTotal = srv.mem_total_gb || 64.0;
  const memUsed = srv.mem_used_gb || (Math.round((memTotal * (srv.mem_pct || 0) / 100) * 10) / 10);
  const diskTotal = srv.disk_total_gb || 1024.0;
  const diskUsed = srv.disk_used_gb || (Math.round((diskTotal * (srv.disk_pct || 0) / 100) * 10) / 10);
  const swapTotal = srv.swap_total_gb || 8.0;
  const swapUsed = srv.swap_used_gb || (Math.round((swapTotal * (srv.swap_pct || 0) / 100) * 10) / 10);

  const isConn = Boolean(srv && srv.connected === true);
  const latency = isConn ? (srv.latency_ms ? `${srv.latency_ms} ms` : '<10 ms') : '断开';

  document.getElementById('srvModalTitle').textContent = `${srv.name} · 系统运行详情`;
  document.getElementById('srvModalSubtitle').textContent = `SSH 探针采集主机: ${srv.username || 'root'}@${srv.host}:${srv.port || 22} · 网络延时: ${latency}`;
  
  document.getElementById('srvModalCpu').textContent = `${srv.cpu_pct !== undefined ? srv.cpu_pct : 0}%`;
  document.getElementById('srvModalCpuCores').textContent = `${cores} 逻辑处理器核心`;

  document.getElementById('srvModalMem').textContent = `${srv.mem_pct !== undefined ? srv.mem_pct : 0}%`;
  document.getElementById('srvModalMemVal').textContent = `${memUsed} / ${memTotal} GB`;

  document.getElementById('srvModalSwap').textContent = `${srv.swap_pct !== undefined ? srv.swap_pct : 0}%`;
  document.getElementById('srvModalSwapVal').textContent = `${swapUsed} / ${swapTotal} GB`;

  document.getElementById('srvModalDisk').textContent = `${srv.disk_pct !== undefined ? srv.disk_pct : 0}%`;
  document.getElementById('srvModalDiskVal').textContent = `${diskUsed} / ${diskTotal} GB`;

  document.getElementById('srvModalHostname').textContent = `${srv.hostname || 'Linux OS'} (${srv.os_name || '已适配'})`;
  document.getElementById('srvModalEndpoint').textContent = `${srv.host}:${srv.port || 22}`;
  document.getElementById('srvModalLoad').textContent = `${srv.load_1m || '0.8'}, ${srv.load_5m || '1.1'}, ${srv.load_15m || '0.9'}`;
  const latencyEl = document.getElementById('srvModalLatency');
  if (latencyEl) latencyEl.textContent = latency;
  document.getElementById('srvModalBoundNode').textContent = boundNodeName;

  const uptimeDays = Math.floor((srv.uptime_sec || 86400 * 20) / 86400);
  const uptimeHours = Math.floor(((srv.uptime_sec || 86400 * 20) % 86400) / 3600);
  document.getElementById('srvModalUptime').textContent = `${uptimeDays} 天 ${uptimeHours} 小时`;
  document.getElementById('srvModalTime').textContent = srv.last_updated || new Date().toLocaleTimeString();

  modal.style.display = 'flex';
};

window.openHardwareNodeDetailModal = function(srvId) {
  const srv = state.servers.find(s => s.id === srvId) || state.servers[0];
  if (!srv) {
    showToast('未找到对应的硬件节点数据', 'warning');
    return;
  }
  const modal = document.getElementById('hardwareNodeDetailModalBackdrop');
  if (!modal) return;

  const tel = state.cluster_telemetry.find(t => t.id === srv.id) || {};
  const isConn = tel.connected === true;
  const isConnecting = tel.connected === null || tel.connected === undefined;
  const isOffline = !isConn && !isConnecting;
  const placeholder = isOffline ? '--' : '<span class="loading-placeholder">获取中...</span>';

  document.getElementById('nodeDetailModalTitle').textContent = `${srv.name} · 硬件运行详情`;
  document.getElementById('nodeDetailModalSubtitle').textContent = `带外 BMC 通道: ${srv.user || 'root'}@${srv.ip} · 品牌: ${(srv.brand || 'Dell').toUpperCase()}`;

  document.getElementById('nodeModalCpu').innerHTML = (isConn && tel.max_cpu_temp !== null && tel.max_cpu_temp !== undefined) ? `${tel.max_cpu_temp} °C` : placeholder;
  document.getElementById('nodeModalInlet').innerHTML = (isConn && tel.inlet_temp !== null && tel.inlet_temp !== undefined) ? `${tel.inlet_temp} °C` : placeholder;
  document.getElementById('nodeModalSpeed').innerHTML = (isConn && tel.fan_target_pct !== null && tel.fan_target_pct !== undefined) ? `${tel.fan_target_pct}%` : placeholder;
  document.getElementById('nodeModalRpm').innerHTML = (isConn && tel.avg_fan_rpm) ? `${tel.avg_fan_rpm} RPM` : (isOffline ? '-- RPM' : '<span class="loading-placeholder">获取中...</span>');

  const pInfo = tel.power || (state.active_server && state.active_server.id === srv.id ? state.power : null);
  document.getElementById('nodeModalPower').innerHTML = (isConn && pInfo && pInfo.total_watts !== null && pInfo.total_watts !== undefined) ? `${pInfo.total_watts} W` : placeholder;

  document.getElementById('nodeModalBrand').textContent = (srv.brand || 'Dell').toUpperCase();
  document.getElementById('nodeModalModel').textContent = srv.model || (srv.brand === 'dell' ? 'PowerEdge Server' : 'Generic Server');
  document.getElementById('nodeModalSerial').textContent = srv.serial || '无 / 未采集';
  document.getElementById('nodeModalIp').textContent = srv.ip || '--';

  const modeMap = { dynamic: '曲线温控', manual: '手动固定', preset: '情景方案', auto: '原厂托管' };
  document.getElementById('nodeModalMode').textContent = modeMap[srv.mode || 'auto'] || srv.mode || '原厂托管';
  document.getElementById('nodeModalLatency').textContent = isConn ? `${tel.latency_ms || 10} ms` : (isConnecting ? '连接探测中' : '离线未连');
  document.getElementById('nodeModalStatus').textContent = isConn ? '联机受控 (正常)' : (isConnecting ? '正在握手获取数据...' : `离线 (${tel.error_msg || '握手超时'})`);
  document.getElementById('nodeModalTime').textContent = tel.last_updated || new Date().toLocaleTimeString();

  modal.style.display = 'flex';
};

function initServerDetailModalEvents() {
  const modal = document.getElementById('serverDetailModalBackdrop');
  const closeBtn = document.getElementById('btnCloseServerDetailModal');
  const confirmBtn = document.getElementById('btnConfirmCloseServerDetail');

  const hide = () => { if (modal) modal.style.display = 'none'; };
  if (closeBtn) closeBtn.addEventListener('click', hide);
  if (confirmBtn) confirmBtn.addEventListener('click', hide);
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) hide();
    });
  }

  // Hardware Node Detail Modal Events
  const nodeModal = document.getElementById('hardwareNodeDetailModalBackdrop');
  const nodeCloseBtn = document.getElementById('btnCloseHardwareNodeDetailModal');
  const nodeConfirmBtn = document.getElementById('btnConfirmCloseHardwareNodeDetail');
  const hideNode = () => { if (nodeModal) nodeModal.style.display = 'none'; };
  if (nodeCloseBtn) nodeCloseBtn.addEventListener('click', hideNode);
  if (nodeConfirmBtn) nodeConfirmBtn.addEventListener('click', hideNode);
  if (nodeModal) {
    nodeModal.addEventListener('click', (e) => {
      if (e.target === nodeModal) hideNode();
    });
  }
}
function renderServerSubsystemsHUD(srvId, container) {
  const srv = state.servers.find(s => s.id === srvId);
  const cfgSubs = srv?.subsystems || [];
  const liveSubs = state.subsystems[srvId] || [];

  if (cfgSubs.length === 0) {
    container.innerHTML = `
      <div style="font-size:10.5px; color:var(--text-tertiary); display:flex; align-items:center; justify-content:space-between; padding:2px 4px;">
        <span>未绑定内部子系统</span>
        <button class="secondary-btn" style="font-size:10px; padding:1px 6px;" onclick="editServerModal('${srvId}')">+ 绑定子系统</button>
      </div>
    `;
    return;
  }

  const isOpen = !!state.subsystem_drawer_open[srvId];
  const onlineCount = liveSubs.filter(s => s.connected).length;

  let html = `
    <button class="subsystem-drawer-toggle" onclick="toggleSubsystemsDrawer('${srvId}')">
      <span style="display:flex; align-items:center; gap:6px;">
        <span class="status-dot" style="background:${onlineCount > 0 ? 'var(--system-green)' : 'var(--system-orange)'}; width:6px; height:6px;"></span>
        <strong>🖥️ 内部子系统 (${onlineCount}/${cfgSubs.length})</strong>
      </span>
      <span style="font-size:10px; color:var(--text-tertiary);">${isOpen ? '收起 ▲' : '展开HUD ▼'}</span>
    </button>
  `;

  if (isOpen) {
    html += cfgSubs.map(cs => {
      const live = liveSubs.find(ls => ls.id === cs.id) || {};
      const isConn = live.connected;

      const cpuPct = isConn ? live.cpu_pct : 0;
      const memPct = isConn ? live.mem_pct : 0;
      const swapPct = isConn ? live.swap_pct : 0;
      const diskPct = isConn ? live.disk_pct : 0;

      const getFillClass = (pct) => {
        if (pct >= 90) return 'red';
        if (pct >= 75) return 'orange';
        return 'blue';
      };

      return `
        <div class="subsystem-mini-card">
          <div class="subsystem-mini-header">
            <span style="font-weight:600; color:var(--text-primary); display:flex; align-items:center; gap:5px;">
              <span class="status-dot" style="background:${isConn ? 'var(--system-green)' : 'var(--system-gray)'}; width:5px; height:5px;"></span>
              ${cs.name}
            </span>
            <span style="font-size:10px; color:${isConn ? 'var(--system-green)' : 'var(--text-tertiary)'};">
              ${isConn ? (live.hostname || `${cs.host}:${cs.port}`) : (live.last_error ? '连接异常' : '连接中...')}
            </span>
          </div>

          ${isConn ? `
            <div class="subsystem-mini-bars-grid">
              <!-- CPU -->
              <div class="subsystem-bar-item">
                <div class="subsystem-bar-label">
                  <span>CPU (${live.cpu_cores}核)</span>
                  <span style="font-weight:600;">${cpuPct}%</span>
                </div>
                <div class="subsystem-progress-track">
                  <div class="subsystem-progress-fill ${getFillClass(cpuPct)}" style="width:${cpuPct}%;"></div>
                </div>
              </div>

              <!-- RAM -->
              <div class="subsystem-bar-item">
                <div class="subsystem-bar-label">
                  <span>内存 (${live.mem_used_gb}/${live.mem_total_gb}G)</span>
                  <span style="font-weight:600;">${memPct}%</span>
                </div>
                <div class="subsystem-progress-track">
                  <div class="subsystem-progress-fill ${getFillClass(memPct)}" style="width:${memPct}%;"></div>
                </div>
              </div>

              <!-- Swap -->
              <div class="subsystem-bar-item">
                <div class="subsystem-bar-label">
                  <span>Swap (${live.swap_used_gb}/${live.swap_total_gb}G)</span>
                  <span style="font-weight:600;">${swapPct}%</span>
                </div>
                <div class="subsystem-progress-track">
                  <div class="subsystem-progress-fill ${swapPct > 80 ? 'red' : 'orange'}" style="width:${swapPct}%;"></div>
                </div>
              </div>

              <!-- Disk -->
              <div class="subsystem-bar-item">
                <div class="subsystem-bar-label">
                  <span>根磁盘 (/)</span>
                  <span style="font-weight:600;">${diskPct}%</span>
                </div>
                <div class="subsystem-progress-track">
                  <div class="subsystem-progress-fill ${getFillClass(diskPct)}" style="width:${diskPct}%;"></div>
                </div>
              </div>
            </div>
          ` : `
            <div style="font-size:10px; color:var(--text-tertiary); padding:2px 0;">
              ${live.last_error ? `报错: ${live.last_error.substring(0, 45)}` : '等待 SSH 探针握手数据流...'}
            </div>
          `}
        </div>
      `;
    }).join('');
  }

  container.innerHTML = html;
}

window.toggleSubsystemsDrawer = function(srvId) {
  state.subsystem_drawer_open[srvId] = !state.subsystem_drawer_open[srvId];
  const subWrapper = document.getElementById(`subsystemsWrapper_${srvId}`);
  if (subWrapper) {
    renderServerSubsystemsHUD(srvId, subWrapper);
  }
};
window.setServerThermalMode = async function(srvId, mode) {
  const srv = state.servers.find(s => s.id === srvId);
  const srvName = srv ? srv.name : srvId;
  const modeLabels = { 'auto': '原厂托管', 'dynamic': '曲线温控', 'manual': '手动全局', 'preset': '情景方案' };
  const label = modeLabels[mode] || mode;

  showToast(`正在对 [${srvName}] 设定 ${label}...`, 'info');

  let res;
  if (mode === 'manual') {
    const globalSp = parseInt(state.config?.ipmi?.manual_speed, 10);
    const sp = srv?.manual_speed || (Number.isFinite(globalSp) && globalSp > 0 ? globalSp : 25);
    res = await callApi('set_all_fans_speed', sp, srvId);
  } else if (mode === 'preset') {
    const globalPk = state.config?.ipmi?.preset_key || 'silent';
    const pk = srv?.preset_key || globalPk;
    res = await callApi('apply_preset', pk, srvId);
  } else {
    res = await callApi('set_fan_mode', mode, srvId);
  }

  if (res && res.success) {
    showToast(res.message || `[${srvName}] 已切换至 ${label}`, 'success');
    if (srv) srv.mode = mode;
    await refreshAllData();
  } else {
    showToast(res?.message || res?.error || '设置温控模式失败', 'error');
  }
};

window.switchProbeLayoutStyle = async function(style) {
  state.probeLayoutStyleUserModified = true;
  state.probeLayoutStyle = style;
  if (state.config && state.config.ipmi) {
    state.config.ipmi.probe_layout_style = style;
  }
  renderProbeClusterMatrix();
  await callApi('set_probe_layout_style', style);
};

window.focusServerDetail = function(srvId) {
  // If user was on another tab (e.g. servers management), switch to dashboard
  if (state.activeTab !== 'dashboard') {
    const dashTabBtn = document.querySelector('.nav-item[data-tab="dashboard"]');
    if (dashTabBtn) dashTabBtn.click();
  }

  const srv = state.servers.find(s => s.id === srvId) || state.servers[0];
  const srvName = srv ? srv.name : srvId;

  setDashboardView('detail');

  state.activeServer = srv;
  const focusedSelect = document.getElementById('focusedServerSelect');
  if (focusedSelect && srv) focusedSelect.value = srv.id;

  const tel = state.cluster_telemetry.find(t => t.id === srvId);
  if (tel) {
    state.connected = !!tel.connected;
    state.latency_ms = tel.latency_ms || 0;
    state.mode = tel.mode || srv?.mode || 'auto';
    state.max_cpu_temp = tel.max_cpu_temp;
    state.inlet_temp = tel.inlet_temp;
    state.cpu_temps = tel.cpu_temps || [];
    state.fans = tel.fans || [];
    state.all_sensors = tel.all_sensors || [];
    state.power = tel.power || { total_watts: null, ps1: {}, ps2: {} };
    state.current_target_speed = tel.fan_target_pct;
    if (!state.connected) {
      state.max_cpu_temp = null;
      state.inlet_temp = null;
      state.cpu_temps = [];
      state.fans = [];
      state.all_sensors = [];
      state.power = { total_watts: null, ps1: {}, ps2: {} };
      state.current_target_speed = null;
    }
  }

  const statusBadge = document.getElementById('detailServerStatusBadge');
  if (statusBadge && srv) {
    statusBadge.className = srv.enabled ? 'badge badge-normal' : 'badge badge-warning';
    statusBadge.textContent = srv.enabled ? '● 联机受控' : '● 未启用';
  }

  // 零等待即刻重绘，0ms 响应用户点击！
  renderDashboardMetrics();
  renderSensorsTable();
  showToast(`已展开 [${srvName}] 单机详情与全量参数`, 'info');

  // 后台无阻塞异步通知 Python 同步
  callApi('switch_server', srvId).then(res => {
    if (res && res.success && res.data) {
      applyStatusData(res.data);
    }
  });
};

// ==========================================
// Multi-Node Curve View
// ==========================================
function initCurveEditor() {
  document.getElementById('btnAddCurveNode').addEventListener('click', async () => {
    if (state.curve_nodes.length >= 10) {
      showToast('最多支持配置 10 个温控节点', 'error');
      return;
    }
    readNodesFromUI();
    const nodes = [...state.curve_nodes].sort((a, b) => a.temp - b.temp);
    const lastNonSafety = nodes[nodes.length - 2] || nodes[0];
    const newTemp = Math.min(80, Math.max(30, lastNonSafety.temp + 4));
    const newSpeed = Math.min(95, lastNonSafety.speed + 12);

    state.curve_nodes.splice(nodes.length - 1, 0, {
      temp: newTemp,
      speed: newSpeed,
      name: `温控阶段 ${state.curve_nodes.length}`
    });
    // Persist immediately so background polling never overwrites it!
    await callApi('set_curve_nodes', state.curve_nodes);
    if (state.config) state.config.curve_nodes = [...state.curve_nodes];
    renderCurveView();
    showToast('已新增温控节点并实时保存！可在下方微调参数', 'success');
  });

  document.getElementById('btnResetDefaultCurve').addEventListener('click', async () => {
    state.curve_nodes = [
      { temp: 45, speed: 15, name: "静音基准" },
      { temp: 55, speed: 22, name: "日常轻载" },
      { temp: 65, speed: 32, name: "中载巡航" },
      { temp: 72, speed: 45, name: "温升加速" },
      { temp: 78, speed: 70, name: "重载强冷" },
      { temp: 82, speed: 100, is_safety: true, name: "BMC熔断保护" }
    ];
    await callApi('set_curve_nodes', state.curve_nodes);
    if (state.config) state.config.curve_nodes = [...state.curve_nodes];
    renderCurveView();
    showToast('已重置为官方推荐多节点温控方案并同步保存！', 'success');
  });

  document.getElementById('btnSaveCurveNodes').addEventListener('click', async () => {
    readNodesFromUI();
    const res = await callApi('set_curve_nodes', state.curve_nodes);
    if (res && res.success) {
      if (state.config) state.config.curve_nodes = [...state.curve_nodes];
      showToast('保存成功', 'success');
      renderCurveView();
    } else {
      showToast(res?.message || '保存失败', 'error');
    }
  });
}

function readNodesFromUI() {
  const cards = document.querySelectorAll('#curveNodesListContainer .node-card');
  const updatedNodes = [];
  cards.forEach((card, idx) => {
    const tempInput = card.querySelector('.node-temp-input');
    const speedInput = card.querySelector('.node-speed-input');
    const isSafety = card.classList.contains('safety-card');
    const nameInput = card.querySelector('.node-title-input');
    const nameSpan = card.querySelector('.node-title-text');

    const nodeName = nameInput ? nameInput.value.trim() : (nameSpan ? nameSpan.textContent.trim() : `阶段 #${idx + 1}`);

    if (tempInput && speedInput) {
      updatedNodes.push({
        temp: parseInt(tempInput.value, 10) || 50,
        speed: parseInt(speedInput.value, 10) || 20,
        is_safety: isSafety,
        name: nodeName || `阶段 #${idx + 1}`
      });
    }
  });
  if (updatedNodes.length >= 2) {
    state.curve_nodes = updatedNodes.sort((a, b) => a.temp - b.temp);
  }
}

window.editCurveNodeName = function(idx) {
  const titleContainer = document.getElementById(`nodeTitleContainer_${idx}`);
  if (!titleContainer) return;
  const currentName = state.curve_nodes[idx]?.name || `阶段 #${idx + 1}`;

  titleContainer.innerHTML = `
    <input type="text" class="node-title-input" id="nodeNameInput_${idx}" value="${currentName}" maxlength="20">
  `;
  const input = document.getElementById(`nodeNameInput_${idx}`);
  if (input) {
    input.focus();
    input.select();

    const saveName = async () => {
      const newName = input.value.trim() || `阶段 #${idx + 1}`;
      if (state.curve_nodes[idx]) {
        state.curve_nodes[idx].name = newName;
      }
      readNodesFromUI();
      await callApi('set_curve_nodes', state.curve_nodes);
      if (state.config) state.config.curve_nodes = [...state.curve_nodes];
      renderCurveView();
      showToast(`节点名称已更新为「${newName}」并已保存`, 'success');
    };

    input.addEventListener('blur', saveName);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        input.blur();
      } else if (e.key === 'Escape') {
        renderCurveView();
      }
    });
  }
};

function renderCurveView() {
  const container = document.getElementById('curveNodesListContainer');
  const badge = document.getElementById('nodesCountBadge');
  if (!container) return;

  const sortedNodes = [...state.curve_nodes].sort((a, b) => a.temp - b.temp);
  badge.textContent = sortedNodes.length;

  container.innerHTML = sortedNodes.map((node, idx) => {
    const isFirst = idx === 0;
    const isLast = idx === sortedNodes.length - 1;
    const isSafety = node.is_safety || isLast;
    const nodeName = node.name || `阶段 #${idx + 1}`;

    return `
      <div class="node-card ${isSafety ? 'safety-card' : ''}">
        <div class="node-header">
          <div id="nodeTitleContainer_${idx}">
            <span class="node-title" onclick="editCurveNodeName(${idx})" title="点击自定义修改此阶段名称">
              <span class="node-title-text">${nodeName}</span>
              <svg class="node-title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
              </svg>
            </span>
          </div>
          ${isSafety 
            ? `<span class="node-safety-badge">BMC 熔断</span>` 
            : (!isFirst && !isLast 
                ? `<button class="node-delete-btn" title="删除该节点" onclick="deleteCurveNode(${idx})">
                    <svg viewBox="0 0 10 10"><path d="M2.5 2.5L7.5 7.5M7.5 2.5L2.5 7.5" stroke="currentColor" stroke-width="1.3"/></svg>
                   </button>` 
                : `<span style="font-size:11px; color:var(--text-tertiary);">基准端点</span>`)}
        </div>

        <div class="node-fields-row">
          <div class="node-field-group">
            <label>触发温度 (°C)</label>
            <div class="node-input-unit">
              <input type="number" class="apple-input node-temp-input" min="30" max="95" value="${node.temp}">
              <span style="font-size:11px; color:var(--text-secondary);">°C</span>
            </div>
          </div>

          <div class="node-field-group">
            <label>风扇转速 (%)</label>
            <div class="node-input-unit">
              <input type="number" class="apple-input node-speed-input" min="5" max="100" value="${node.speed}">
              <span style="font-size:11px; color:var(--text-secondary);">%</span>
            </div>
          </div>
        </div>
      </div>
    `;
  }).join('');

  renderCurveSvg(sortedNodes);
}

window.deleteCurveNode = async function(index) {
  readNodesFromUI();
  if (state.curve_nodes.length <= 2) {
    showToast('温控曲线至少需要保留 2 个端点', 'error');
    return;
  }
  state.curve_nodes.splice(index, 1);
  await callApi('set_curve_nodes', state.curve_nodes);
  if (state.config) state.config.curve_nodes = [...state.curve_nodes];
  renderCurveView();
  showToast('节点已移除并同步保存');
};

function renderCurveSvg(nodes) {
  const svg = document.getElementById('curveSvg');
  if (!svg) return;

  const w = 700;
  const h = 240;
  const padL = 40;
  const padR = 30;
  const padT = 25;
  const padB = 30;

  const minT = 30;
  const maxT = 95;
  const minS = 0;
  const maxS = 100;

  const toX = t => padL + ((t - minT) / (maxT - minT)) * (w - padL - padR);
  const toY = s => h - padB - ((s - minS) / (maxS - minS)) * (h - padT - padB);

  // Background Grid
  let gridLines = '';
  for (let t = 40; t <= 90; t += 10) {
    const x = toX(t);
    gridLines += `<line x1="${x}" y1="${padT}" x2="${x}" y2="${h - padB}" stroke="var(--border-subtle)" stroke-width="1"/>`;
    gridLines += `<text x="${x}" y="${h - 12}" fill="var(--text-tertiary)" font-size="10" text-anchor="middle">${t}°C</text>`;
  }
  for (let s = 20; s <= 100; s += 20) {
    const y = toY(s);
    gridLines += `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="var(--border-subtle)" stroke-width="1"/>`;
    gridLines += `<text x="${padL - 8}" y="${y + 3}" fill="var(--text-tertiary)" font-size="10" text-anchor="end">${s}%</text>`;
  }

  // Points & Path
  let pathD = '';
  let areaD = '';
  const pts = nodes.map(n => ({ x: toX(n.temp), y: toY(n.speed), temp: n.temp, speed: n.speed, is_safety: n.is_safety }));

  if (pts.length > 0) {
    pathD = `M ${pts[0].x} ${pts[0].y}`;
    areaD = `M ${pts[0].x} ${h - padB} L ${pts[0].x} ${pts[0].y}`;
    for (let i = 1; i < pts.length; i++) {
      pathD += ` L ${pts[i].x} ${pts[i].y}`;
      areaD += ` L ${pts[i].x} ${pts[i].y}`;
    }
    areaD += ` L ${pts[pts.length - 1].x} ${h - padB} Z`;
  }

  // Dynamic Current Operating Point
  let currentOperatingPoint = '';
  if (state.connected && state.max_cpu_temp !== null) {
    const curX = Math.min(w - padR, Math.max(padL, toX(state.max_cpu_temp)));
    const curY = Math.min(h - padB, Math.max(padT, toY(state.current_target_speed || 25)));
    currentOperatingPoint = `
      <g>
        <circle cx="${curX}" cy="${curY}" r="12" fill="var(--system-green)" opacity="0.25">
          <animate attributeName="r" values="8;16;8" dur="2s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.35;0.1;0.35" dur="2s" repeatCount="indefinite"/>
        </circle>
        <circle cx="${curX}" cy="${curY}" r="5" fill="var(--system-green)" stroke="#FFFFFF" stroke-width="2"/>
        <text x="${curX}" y="${curY - 10}" fill="var(--system-green)" font-size="11" font-weight="700" text-anchor="middle">
          当前工况 (${state.max_cpu_temp}°C, ${state.current_target_speed}%)
        </text>
      </g>
    `;
  }

  // Safety threshold vertical red guard line
  const safetyNode = nodes[nodes.length - 1];
  const safeX = toX(safetyNode.temp);
  const safetyLine = `
    <line x1="${safeX}" y1="${padT}" x2="${safeX}" y2="${h - padB}" stroke="var(--system-red)" stroke-width="1.5" stroke-dasharray="4 3"/>
    <text x="${safeX - 4}" y="${padT + 12}" fill="var(--system-red)" font-size="10" font-weight="600" text-anchor="end">BMC 熔断线</text>
  `;

  // Dots for each node
  const nodeDots = pts.map(p => `
    <circle cx="${p.x}" cy="${p.y}" r="4.5" fill="${p.is_safety ? 'var(--system-red)' : 'var(--system-blue)'}" stroke="#FFFFFF" stroke-width="2"/>
    <text x="${p.x}" y="${p.y - 8}" fill="var(--text-secondary)" font-size="10" font-weight="600" text-anchor="middle">${p.temp}°C, ${p.speed}%</text>
  `).join('');

  svg.innerHTML = `
    <defs>
      <linearGradient id="curveAreaGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="var(--system-blue)" stop-opacity="0.25"/>
        <stop offset="100%" stop-color="var(--system-blue)" stop-opacity="0.0"/>
      </linearGradient>
    </defs>
    ${gridLines}
    <path d="${areaD}" fill="url(#curveAreaGrad)"/>
    <path d="${pathD}" fill="none" stroke="var(--system-blue)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
    ${safetyLine}
    ${nodeDots}
    ${currentOperatingPoint}
  `;
}

// ==========================================
// Fan Matrix Tab (手动调速：全局调速 / 独立调速 左右胶囊切换，保存后同步策略)
// ==========================================
let currentManualModeType = 'global'; // 'global' or 'individual'

function initFanMatrix() {
  const container = document.getElementById('fanChannelsGrid');
  if (!container) return;

  const currentManualSpeed = parseInt(state.config?.ipmi?.manual_speed, 10) || 25;

  // 1. 初始化 6 通道风扇输入框与滑块
  let html = '';
  for (let i = 0; i < 6; i++) {
    const chSpeed = parseInt(state.config?.ipmi?.[`fan${i + 1}_speed`], 10) || currentManualSpeed;
    html += `
      <div class="channel-card">
        <div class="channel-header">
          <div class="channel-title-row">
            <span class="channel-name">通道 #${i + 1} 风扇</span>
            <span class="channel-rpm" id="channelRpm_${i}">-- RPM</span>
          </div>
          <div class="node-input-unit">
            <input type="number" class="apple-input channel-val-input" id="fanInput_${i}" min="5" max="100" value="${chSpeed}">
            <span style="font-size:12px; color:var(--text-secondary);">%</span>
          </div>
        </div>
        <input type="range" class="apple-slider" id="fanRange_${i}" min="5" max="100" value="${chSpeed}">
      </div>
    `;
  }
  container.innerHTML = html;

  for (let i = 0; i < 6; i++) {
    const range = document.getElementById(`fanRange_${i}`);
    const input = document.getElementById(`fanInput_${i}`);
    if (range && input) {
      range.addEventListener('input', e => {
        input.value = e.target.value;
      });
      input.addEventListener('input', e => {
        const val = Math.max(5, Math.min(100, parseInt(e.target.value, 10) || 25));
        range.value = val;
      });
    }
  }

  // 2. 全局调速 与 独立调速 胶囊左右切换监听
  const manualSeg = document.getElementById('manualModeSegment');
  const globalSec = document.getElementById('manualGlobalSection');
  const indSec = document.getElementById('manualIndividualSection');

  if (manualSeg) {
    manualSeg.querySelectorAll('.seg-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const type = btn.getAttribute('data-manual-type');
        currentManualModeType = type;

        manualSeg.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        if (type === 'global') {
          if (globalSec) globalSec.style.display = 'block';
          if (indSec) indSec.style.display = 'none';
        } else {
          if (globalSec) globalSec.style.display = 'none';
          if (indSec) indSec.style.display = 'block';
        }
      });
    });
  }

  // 3. 点击「保存策略并同步」按钮
  const btnSave = document.getElementById('btnSaveManualStrategy');
  if (btnSave) {
    btnSave.addEventListener('click', async () => {
      btnSave.disabled = true;
      const originalHtml = btnSave.innerHTML;
      btnSave.innerHTML = `
        <span class="spinner-inline" style="display:inline-block; width:12px; height:12px; border:2px solid rgba(255,255,255,0.3); border-top-color:#fff; border-radius:50%; animation:spin 0.8s linear infinite; margin-right:4px;"></span>
        <span>正在保存并同步...</span>
      `;

      try {
        let res;
        if (currentManualModeType === 'global') {
          const gVal = parseInt(document.getElementById('tabGlobalFanSlider')?.value, 10) || 25;
          res = await callApi('save_manual_speed_strategy', 'global', gVal, []);
        } else {
          const chVals = [];
          for (let i = 0; i < 6; i++) {
            const v = parseInt(document.getElementById(`fanRange_${i}`)?.value, 10) || 25;
            chVals.push(v);
          }
          res = await callApi('save_manual_speed_strategy', 'individual', 25, chVals);
        }

        if (res && res.success) {
          showToast('保存成功', 'success');
          await refreshAllData();
        } else {
          showToast(res?.message || res?.error || '保存失败', 'error');
        }
      } catch (err) {
        showToast('保存失败', 'error');
      } finally {
        btnSave.disabled = false;
        btnSave.innerHTML = originalHtml;
      }
    });
  }

  // 4. 全局风扇滑块与快捷档位联动
  const globalSlider = document.getElementById('tabGlobalFanSlider');
  const globalBadge = document.getElementById('tabGlobalFanValBadge');
  const globalRpm = document.getElementById('tabGlobalFanRpmEst');
  if (globalSlider) {
    globalSlider.addEventListener('input', (e) => {
      const v = parseInt(e.target.value, 10);
      if (globalBadge) globalBadge.textContent = `${v}%`;
      if (globalRpm) globalRpm.textContent = `约 ${Math.round(1200 + (v / 100) * 11500)} RPM`;
      // 同时联动各通道默认显示
      for (let i = 0; i < 6; i++) {
        const inp = document.getElementById(`fanInput_${i}`);
        const rng = document.getElementById(`fanRange_${i}`);
        if (inp && rng) {
          inp.value = v;
          rng.value = v;
        }
      }
    });
  }

  window.setTabGlobalSpeed = function(val) {
    if (globalSlider) {
      globalSlider.value = val;
      if (globalBadge) globalBadge.textContent = `${val}%`;
      if (globalRpm) globalRpm.textContent = `约 ${Math.round(1200 + (val / 100) * 11500)} RPM`;
    }
    for (let i = 0; i < 6; i++) {
      const inp = document.getElementById(`fanInput_${i}`);
      const rng = document.getElementById(`fanRange_${i}`);
      if (inp && rng) {
        inp.value = val;
        rng.value = val;
      }
    }
  };

  // 仅在首次载入或用户主动切换到手动页面时初始化回显，绝不在用户拖动滑块时被后台轮询重置
  let manualTabInitialized = false;
  window.syncManualTabSliders = function(force = false) {
    if (!force && manualTabInitialized) return;
    const sp = parseInt(state.config?.ipmi?.manual_speed, 10);
    if (Number.isFinite(sp) && sp > 0) {
      if (globalSlider && document.activeElement !== globalSlider) {
        globalSlider.value = sp;
        if (globalBadge) globalBadge.textContent = `${sp}%`;
        if (globalRpm) globalRpm.textContent = `约 ${Math.round(1200 + (sp / 100) * 11500)} RPM`;
      }
    }
    const modeType = state.config?.ipmi?.manual_mode_type || 'global';
    currentManualModeType = modeType;
    if (manualSeg) {
      manualSeg.querySelectorAll('.seg-btn').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-manual-type') === modeType);
      });
      if (globalSec) globalSec.style.display = modeType === 'global' ? 'block' : 'none';
      if (indSec) indSec.style.display = modeType === 'individual' ? 'block' : 'none';
    }
    manualTabInitialized = true;
  };
  window.syncManualTabSliders(true);
}

// ==========================================
// Presets Tab (Auto-synchronizes to all servers currently in preset strategy)
// ==========================================
function initPresetsCatalog() {
  const container = document.getElementById('presetsGrid');
  if (!container) return;

  const currentPreset = state.servers.find(s => s.mode === 'preset')?.preset_key || state.activeServer?.preset_key || 'silent';

  container.innerHTML = PRESETS.map(p => {
    const isCurrent = p.key === currentPreset;
    return `
      <div class="preset-card ${isCurrent ? 'active' : ''}">
        <div>
          <div class="preset-header">
            <div class="preset-icon-badge">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${p.iconSvg}</svg>
            </div>
            <div>
              <div class="preset-title" style="display:flex; align-items:center; gap:6px;">
                <span>${p.title}</span>
                ${isCurrent ? `<span class="badge badge-normal" style="font-size:10.5px; padding:1px 6px;">正在生效</span>` : ''}
              </div>
              <div class="preset-speed-tags">
                ${p.speeds.map((s, idx) => `<span class="speed-tag">F${idx + 1}:${s}%</span>`).join('')}
              </div>
            </div>
          </div>
          <p class="preset-desc" style="margin-top:10px;">${p.desc}</p>
        </div>

        <button class="primary-btn preset-btn" onclick="selectPresetScheme('${p.key}')">
          ${isCurrent ? '正在生效中' : '选用此方案'}
        </button>
      </div>
    `;
  }).join('');
}

window.selectPresetScheme = async function(key) {
  const preset = PRESETS.find(p => p.key === key);
  const title = preset ? preset.title : key;
  const res = await callApi('update_preset_strategy', key);
  if (res && res.success) {
    showToast(res.message || `已选用「${title}」，并自动同步至使用方案模式的服务器`, 'success');
    for (const srv of state.servers) {
      if (srv.mode === 'preset') srv.preset_key = key;
    }
    if (state.activeServer) state.activeServer.preset_key = key;
    initPresetsCatalog();
    await refreshAllData();
  } else {
    showToast(res?.message || '选用方案失败', 'error');
  }
};

// ==========================================
// Sensor Telemetry Tab
// ==========================================
function initSensorsTab() {
  const searchInput = document.getElementById('sensorSearchInput');
  searchInput.addEventListener('input', (e) => {
    state.sensorSearchTerm = e.target.value.toLowerCase().trim();
    renderSensorsTable();
  });

  const catBtns = document.querySelectorAll('#sensorCategoryTabs .cat-btn');
  catBtns.forEach(b => {
    b.addEventListener('click', () => {
      catBtns.forEach(cb => cb.classList.remove('active'));
      b.classList.add('active');
      state.sensorCategory = b.dataset.cat;
      renderSensorsTable();
    });
  });
}

function formatSensorItem(s) {
  const rawName = (s.name || '').trim();
  const lower = rawName.toLowerCase();
  let cn = rawName;
  let priority = 50; // 数值越小排在越前
  let isCore = false;

  // CPU 核心温度
  if (lower.includes('cpu1 temp') || lower.includes('cpu 1 temp') || lower.includes('temp (cpu1)')) {
    cn = 'CPU 1 核心温度';
    priority = 1;
    isCore = true;
  } else if (lower.includes('cpu2 temp') || lower.includes('cpu 2 temp') || lower.includes('temp (cpu2)')) {
    cn = 'CPU 2 核心温度';
    priority = 2;
    isCore = true;
  } else if (lower.startsWith('cpu') && lower.includes('temp')) {
    cn = `${rawName.toUpperCase()} 核心温度`;
    priority = 3;
    isCore = true;
  } else if (lower.includes('inlet temp') || lower === 'inlet') {
    cn = '机箱进风口温度';
    priority = 4;
    isCore = true;
  } else if (lower.includes('exhaust temp') || lower === 'exhaust') {
    cn = '机箱出风口温度';
    priority = 5;
    isCore = true;
  } else if (lower.includes('system board temp') || lower.includes('planar temp')) {
    cn = '主板芯片组温度';
    priority = 6;
  } else {
    // 风扇转速
    const fanMatch = rawName.match(/fan\s*(\d+)([a-zA-Z]?)\s*rpm/i);
    if (fanMatch) {
      const num = fanMatch[1];
      const sub = fanMatch[2] ? ` (${fanMatch[2].toUpperCase()}路)` : '';
      cn = `风扇 #${num}${sub} 转速`;
      priority = 10 + parseInt(num, 10);
      isCore = true;
    } else if (lower.includes('fan') && lower.includes('rpm')) {
      cn = `${rawName} 转速`;
      priority = 20;
      isCore = true;
    } else if (lower.includes('pwr consumption') || lower.includes('system level')) {
      cn = '整机总功耗 (直接获取)';
      priority = 7;
      isCore = true;
    } else if (lower.includes('ps1 current') || lower.includes('ps 1 current')) {
      cn = 'PS1 实时工作电流';
      priority = 31;
    } else if (lower.includes('ps2 current') || lower.includes('ps 2 current')) {
      cn = 'PS2 实时工作电流';
      priority = 32;
    } else if (lower.includes('ps1 voltage') || lower.includes('ps 1 voltage')) {
      cn = 'PS1 供电电压';
      priority = 33;
    } else if (lower.includes('ps2 voltage') || lower.includes('ps 2 voltage')) {
      cn = 'PS2 供电电压';
      priority = 34;
    } else if (lower.includes('current 1')) {
      cn = 'PS1 供电电流';
      priority = 35;
    } else if (lower.includes('current 2')) {
      cn = 'PS2 供电电流';
      priority = 36;
    } else if (lower.includes('voltage 1')) {
      cn = '主供电母线电压';
      priority = 37;
    } else if (lower.includes('fan redundancy')) {
      cn = '风扇阵列就绪状态';
      priority = 40;
    } else if (lower.includes('ps redundancy')) {
      cn = '双电源就绪状态';
      priority = 41;
    } else if (lower.includes('dimm') || lower.includes('mem')) {
      cn = `内存模组状态 [${rawName}]`;
      priority = 45;
    } else if (lower.includes('vbat') || lower.includes('battery')) {
      cn = `主板纽扣电池电压 [${rawName}]`;
      priority = 46;
    }
  }

  // 安全告警参考阈值展示
  let thresholdText = '-';
  if (s.warn_max && s.warn_max !== 'na') {
    thresholdText = `< ${s.warn_max} ${s.unit || ''}`;
  } else if (s.fault_max && s.fault_max !== 'na') {
    thresholdText = `< ${s.fault_max} ${s.unit || ''}`;
  } else if (s.warn_min && s.warn_min !== 'na') {
    thresholdText = `> ${s.warn_min} ${s.unit || ''}`;
  }

  return {
    rawName,
    cn,
    priority,
    isCore,
    value: s.value,
    unit: s.unit || '',
    status: s.status || 'ok',
    isOk: (s.status || '').toLowerCase() === 'ok',
    thresholdText
  };
}

function isSensorValueValid(val, unit, status) {
  if (val === null || val === undefined) return false;
  const s = String(val).trim().toLowerCase();
  if (s === '' || s === 'na' || s === 'n/a' || s === 'none' || s === 'null' || s === 'disabled' || s === 'not readable' || s === 'no reading') return false;
  const st = String(status || '').toLowerCase();
  if (st === 'ns' && (s === '0' || s === '0.0' || s === 'na')) return false;
  if (unit && unit.includes('RPM') && (s === '0' || s === '0.0') && st !== 'ok') return false;
  return true;
}

function renderSensorsTable() {
  const tbody = document.getElementById('sensorTableBody');
  if (!tbody) return;

  if (!state.connected || state.all_sensors.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" style="text-align:center; padding:32px; color:var(--text-tertiary);">
          ${state.connected ? '当前服务器暂无可用传感器数据' : '当前节点已离线或未连接，为避免误判已清空展示数据'}
        </td>
      </tr>
    `;
    return;
  }

  // 严格过滤无读数、无状态、未安装插槽的无效/无数值冗余项
  let validSensors = state.all_sensors.filter(s => isSensorValueValid(s.value, s.unit, s.status));

  if (state.sensorCategory === 'temp') {
    validSensors = validSensors.filter(s => s.unit?.includes('degrees C') || s.name.toLowerCase().includes('temp'));
  } else if (state.sensorCategory === 'rpm') {
    validSensors = validSensors.filter(s => s.unit?.includes('RPM') || s.name.toLowerCase().includes('fan'));
  } else if (state.sensorCategory === 'power') {
    validSensors = validSensors.filter(s => s.unit?.includes('Watts') || s.unit?.includes('Volts') || s.unit?.includes('Amps'));
  }

  if (state.sensorSearchTerm) {
    const term = state.sensorSearchTerm.toLowerCase();
    validSensors = validSensors.filter(s => 
      s.name.toLowerCase().includes(term) || 
      (s.unit && s.unit.toLowerCase().includes(term))
    );
  }

  // 中文化对照并按核心指标优先级排序（CPU、进气温、风扇、实时功耗置顶）
  const formattedList = validSensors.map(formatSensorItem);
  formattedList.sort((a, b) => a.priority - b.priority);

  if (formattedList.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" style="text-align:center; padding:32px; color:var(--text-tertiary);">
          没有匹配的有效传感器数据
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = formattedList.map(item => {
    const statusBadge = `<span class="badge ${item.isOk ? 'badge-normal' : 'badge-warning'}">${item.isOk ? '正常' : item.status}</span>`;
    const coreHighlight = item.isCore ? 'style="background:rgba(255,255,255,0.02);"' : '';
    return `
      <tr ${coreHighlight}>
        <td>
          <div style="display:flex; align-items:center; gap:6px;">
            <strong style="color:var(--text-primary); font-size:13px;">${item.cn}</strong>
            ${item.isCore ? '<span class="badge badge-cool" style="font-size:10px; padding:1px 5px;">核心</span>' : ''}
          </div>
          <div style="font-size:11px; color:var(--text-tertiary); font-family:var(--font-mono); margin-top:2px;">${item.rawName}</div>
        </td>
        <td>
          <span class="mono-cell" style="font-size:13.5px; font-weight:700; color:var(--text-primary);">${item.value}</span>
        </td>
        <td>
          <span style="font-size:12px; color:var(--text-secondary);">${item.unit}</span>
        </td>
        <td>${statusBadge}</td>
        <td class="mono-cell" style="color:var(--text-tertiary); font-size:12px;">${item.thresholdText}</td>
      </tr>
    `;
  }).join('');
}

// ==========================================
// Requirement 2: Cluster Management (Tab 5)
// ==========================================
function initServerClusterManagement() {
  // Unified Search input for both hardware nodes and system servers
  const clusterSearch = document.getElementById('clusterSearchInput');
  if (clusterSearch) {
    clusterSearch.addEventListener('input', () => {
      renderServerManagementList();
    });
  }

  // Unified Layout Switcher (Grid / Row) for both hardware nodes and system servers
  const clusterLayoutSwitch = document.getElementById('clusterLayoutSwitch');
  if (clusterLayoutSwitch) {
    clusterLayoutSwitch.querySelectorAll('.seg-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        clusterLayoutSwitch.querySelectorAll('.seg-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.clusterViewMode = btn.getAttribute('data-view');
        renderServerManagementList();
      });
    });
  }

  // Quick Liveness / Probe Interval Setting
  const quickPoll = document.getElementById('quickSubPollInput');
  if (quickPoll) {
    quickPoll.value = state.subsystem_poll_sec || 2;
    quickPoll.addEventListener('change', async () => {
      const val = parseInt(quickPoll.value, 10);
      if (val >= 1 && val <= 60) {
        state.subsystem_poll_sec = val;
        await callApi('set_subsystem_poll_sec', val);
        showToast(`服务器与探针测活周期已更新为 ${val} 秒`, 'success');
      } else {
        quickPoll.value = state.subsystem_poll_sec || 2;
        showToast('测活周期须为 1 ~ 60 秒之间的整数', 'error');
      }
    });
  }

  // Modal 1: 硬件节点
  const btnOpenAddSrv = document.getElementById('btnOpenAddServerModal');
  if (btnOpenAddSrv) {
    btnOpenAddSrv.addEventListener('click', (e) => {
      e.preventDefault();
      triggerOpenAddHardwareNodeModal();
    });
  }

  // 自动嗅探目标 BMC 的硬件厂商、型号与序列号
  const btnAutoProbe = document.getElementById('btnAutoProbeFru');
  if (btnAutoProbe) {
    btnAutoProbe.addEventListener('click', async () => {
      const ip = document.getElementById('srvFormIp').value.trim();
      const user = document.getElementById('srvFormUser').value.trim() || 'root';
      const password = document.getElementById('srvFormPassword').value;

      if (!ip) {
        showToast('请先输入 BMC / iDRAC IP 地址', 'error');
        return;
      }

      const origText = btnAutoProbe.innerHTML;
      btnAutoProbe.disabled = true;
      btnAutoProbe.innerHTML = '<span>⏳ 正在嗅探硬件资产...</span>';
      showToast(`正在连接 ${ip} 探测硬件资产 (FRU/mc info)...`, 'info');

      try {
        const res = await callApi('probe_hardware_fru', ip, user, password, 8);
        if (res && res.success) {
          const brandSel = document.getElementById('srvFormBrand');
          if (brandSel && res.brand) {
            brandSel.value = res.brand;
          }
          if (res.model) {
            document.getElementById('srvFormModel').value = res.model;
          }
          const serialEl = document.getElementById('srvFormSerial');
          if (serialEl && res.serial) {
            serialEl.value = res.serial;
          }
          const nameInput = document.getElementById('srvFormName');
          if (!nameInput.value.trim() && res.model) {
            nameInput.value = `${res.brand_name.split(' ')[0]} ${res.model}`;
          }
          showToast(`已成功识别: ${res.brand_name} · ${res.model || '通用型号'}${res.serial ? ' (SN: ' + res.serial + ')' : ''}`, 'success');
        } else {
          showToast(res?.message || '自动识别未通，请检查 IP、用户名或密码，亦可手动填入', 'warning');
        }
      } catch (err) {
        showToast('嗅探请求异常: ' + (err.message || err), 'error');
      } finally {
        btnAutoProbe.disabled = false;
        btnAutoProbe.innerHTML = origText;
      }
    });
  }

  document.getElementById('btnCancelServerForm').addEventListener('click', closeHardwareNodeModal);
  document.getElementById('btnCloseHardwareNodeModal')?.addEventListener('click', closeHardwareNodeModal);
  document.getElementById('hardwareNodeModalBackdrop')?.addEventListener('click', (e) => {
    if (e.target.id === 'hardwareNodeModalBackdrop') closeHardwareNodeModal();
  });

  // Modal 2: 系统服务器 (SSH)
  const btnAutoProbeOs = document.getElementById('btnAutoProbeOs');
  if (btnAutoProbeOs) {
    btnAutoProbeOs.addEventListener('click', async () => {
      const host = document.getElementById('sysSrvFormHost').value.trim();
      const port = document.getElementById('sysSrvFormPort').value.trim() || 22;
      const user = document.getElementById('sysSrvFormUser').value.trim() || 'root';
      const password = document.getElementById('sysSrvFormPassword').value;

      if (!host) {
        showToast('请先输入 SSH 主机 IP / 域名', 'error');
        return;
      }

      const origText = btnAutoProbeOs.innerHTML;
      btnAutoProbeOs.disabled = true;
      btnAutoProbeOs.innerHTML = '<span>⏳ 正在探测 OS...</span>';
      showToast(`正在连接 ${host}:${port} 探测系统版本...`, 'info');

      try {
        const res = await callApi('probe_system_os', host, port, user, password);
        if (res && res.success && res.os_name) {
          document.getElementById('sysSrvFormOsName').value = res.os_name;
          showToast(res.message || `识别成功: ${res.os_name}`, 'success');
        } else {
          showToast(res?.message || '未能探测到系统，请检查网络或密码', 'warning');
        }
      } catch (err) {
        showToast('探测异常: ' + (err.message || err), 'error');
      } finally {
        btnAutoProbeOs.disabled = false;
        btnAutoProbeOs.innerHTML = origText;
      }
    });
  }

  // 集群管理顶部：快速在线测活与强制刷新全部
  const btnClusterPing = document.getElementById('btnClusterPingAll');
  if (btnClusterPing) {
    btnClusterPing.addEventListener('click', async () => {
      showToast('正在轻量快速测活所有硬件节点与系统服务器...', 'info');
      await callApi('cluster_ping_all');
      await refreshAllData();
      renderServerManagementList();
      showToast('所有节点在线状态测活完成', 'success');
    });
  }

  const btnClusterForceRef = document.getElementById('btnClusterForceRefreshAll');
  if (btnClusterForceRef) {
    btnClusterForceRef.addEventListener('click', async () => {
      showToast('正在强制深度重测并获取所有服务器品牌、型号、出厂SN与系统版本...', 'info');
      const res = await callApi('cluster_force_refresh_all');
      await refreshAllData();
      renderServerManagementList();
      if (res && res.success) {
        showToast(res.message || '全集群资产与系统版本已成功更新', 'success');
      } else {
        showToast(res?.error || '刷新完成', 'info');
      }
    });
  }

  const btnOpenAddSys = document.getElementById('btnOpenAddSystemServerModal');
  if (btnOpenAddSys) {
    btnOpenAddSys.addEventListener('click', (e) => {
      e.preventDefault();
      triggerOpenAddSystemServerModal();
    });
  }

  document.getElementById('btnCancelSysServerForm')?.addEventListener('click', closeSystemServerModal);
  document.getElementById('btnCloseSystemServerModal')?.addEventListener('click', closeSystemServerModal);
  document.getElementById('systemServerModalBackdrop')?.addEventListener('click', (e) => {
    if (e.target.id === 'systemServerModalBackdrop') closeSystemServerModal();
  });

  document.getElementById('btnSaveSysServerForm')?.addEventListener('click', async () => {
    const name = document.getElementById('sysSrvFormName').value.trim();
    const host = document.getElementById('sysSrvFormHost').value.trim();
    const port = parseInt(document.getElementById('sysSrvFormPort').value || 22, 10);
    const username = document.getElementById('sysSrvFormUser').value.trim() || 'root';
    const password = document.getElementById('sysSrvFormPassword').value;
    const node_id = document.getElementById('sysSrvFormNodeSelect').value;
    const os_name = document.getElementById('sysSrvFormOsName')?.value?.trim() || '';

    if (!host) {
      showToast('请输入有效的主机 IP 地址', 'error');
      return;
    }

    if (editingSysServerId) {
      const existing = state.system_servers.find(s => s.id === editingSysServerId);
      if (existing) {
        existing.name = name;
        existing.host = host;
        existing.port = port;
        existing.username = username;
        existing.password = password;
        existing.node_id = node_id;
        existing.os_name = os_name;
      }
      const res = await callApi('update_system_server', editingSysServerId, {
        name, host, port, username, password, node_id, os_name
      });
      if (res && res.success) {
        showToast(res.message, 'success');
      }
    } else {
      const res = await callApi('add_system_server', name, host, port, username, password, node_id, os_name);
      if (res && res.success) {
        showToast(res.message, 'success');
      }
    }

    closeSystemServerModal();
    await refreshAllData();
    renderServerManagementList();
    renderProbeClusterMatrix();
  });

  document.getElementById('btnSaveServerForm').addEventListener('click', async () => {
    const name = document.getElementById('srvFormName').value;
    const brand = document.getElementById('srvFormBrand')?.value || 'dell';
    const model = document.getElementById('srvFormModel').value;
    const serial = document.getElementById('srvFormSerial')?.value || '';
    const ip = document.getElementById('srvFormIp').value;
    const user = document.getElementById('srvFormUser').value;
    const password = document.getElementById('srvFormPassword').value;

    if (!ip.trim()) {
      showToast('请输入有效的 BMC / iDRAC IP 地址', 'error');
      return;
    }

    if (editingServerId) {
      const res = await callApi('update_server', editingServerId, { 
        name, brand, model, serial, ip, user, password
      });
      if (res && res.success) {
        showToast(res.message, 'success');
      }
    } else {
      const res = await callApi('add_server', name, ip, user, password, model, brand, serial);
      if (res && res.success) {
        showToast(res.message, 'success');
      }
    }

    // 点击保存立即平滑关闭弹窗，由后台异步探测，状态与报错直接映射到离线标签
    closeHardwareNodeModal();
    await refreshAllData();
    renderServerManagementList();
    renderProbeClusterMatrix();
  });
}

// ==========================================
// Subsystems Management in Server Form
// ==========================================
function renderServerFormSubsystems() {
  const container = document.getElementById('srvFormSubsystemsList');
  if (!container) return;

  if (state.editing_server_subsystems.length === 0) {
    container.innerHTML = `
      <div style="font-size:12px; color:var(--text-tertiary); text-align:center; padding:12px; background:var(--surface-primary); border-radius:8px; border:1px dashed var(--border-subtle);">
        尚未为该节点添加内部子系统，点击右上角「+ 添加子系统」可绑定 Linux 虚拟机或宿主系统
      </div>
    `;
    return;
  }

  container.innerHTML = state.editing_server_subsystems.map((sub, idx) => `
    <div class="subsystem-config-row" id="subConfigRow_${idx}">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <span style="font-weight:600; font-size:12.5px; color:var(--text-primary); display:flex; align-items:center; gap:6px;">
          <span>🖥️ 子系统 #${idx + 1}</span>
        </span>
        <div style="display:flex; gap:6px;">
          <button type="button" class="secondary-btn" style="font-size:11px; padding:2px 8px;" onclick="testSubsystemSSHRow(${idx})">⚡ 测试SSH</button>
          <button type="button" class="secondary-btn" style="font-size:11px; padding:2px 8px; color:var(--system-red);" onclick="removeSubsystemRow(${idx})">删除</button>
        </div>
      </div>

      <div style="display:grid; grid-template-columns: 1.2fr 1.2fr 0.6fr 1fr 1fr; gap:8px;">
        <div>
          <label style="font-size:10.5px; color:var(--text-tertiary); display:block; margin-bottom:2px;">名称</label>
          <input type="text" class="apple-input" style="font-size:11.5px; padding:4px 8px;" value="${sub.name || ''}" placeholder="如 PVE / Ubuntu" onchange="updateSubsystemField(${idx}, 'name', this.value)">
        </div>
        <div>
          <label style="font-size:10.5px; color:var(--text-tertiary); display:block; margin-bottom:2px;">SSH IP / 域名</label>
          <input type="text" class="apple-input" style="font-size:11.5px; padding:4px 8px;" value="${sub.host || ''}" placeholder="192.168.1.100" onchange="updateSubsystemField(${idx}, 'host', this.value)">
        </div>
        <div>
          <label style="font-size:10.5px; color:var(--text-tertiary); display:block; margin-bottom:2px;">端口</label>
          <input type="number" class="apple-input" style="font-size:11.5px; padding:4px 8px;" value="${sub.port || 22}" min="1" max="65535" onchange="updateSubsystemField(${idx}, 'port', this.value)">
        </div>
        <div>
          <label style="font-size:10.5px; color:var(--text-tertiary); display:block; margin-bottom:2px;">SSH 账户</label>
          <input type="text" class="apple-input" style="font-size:11.5px; padding:4px 8px;" value="${sub.username || 'root'}" placeholder="root" onchange="updateSubsystemField(${idx}, 'username', this.value)">
        </div>
        <div>
          <label style="font-size:10.5px; color:var(--text-tertiary); display:block; margin-bottom:2px;">SSH 密码</label>
          <input type="password" class="apple-input" style="font-size:11.5px; padding:4px 8px;" value="${sub.password || ''}" placeholder="留空为无密码" onchange="updateSubsystemField(${idx}, 'password', this.value)">
        </div>
      </div>
      <div id="subRowStatus_${idx}" style="font-size:11px; margin-top:5px; display:none;"></div>
    </div>
  `).join('');
}

window.updateSubsystemField = function(idx, field, value) {
  if (state.editing_server_subsystems[idx]) {
    state.editing_server_subsystems[idx][field] = value;
  }
};

window.removeSubsystemRow = function(idx) {
  state.editing_server_subsystems.splice(idx, 1);
  renderServerFormSubsystems();
};

window.testSubsystemSSHRow = async function(idx) {
  const sub = state.editing_server_subsystems[idx];
  if (!sub) return;

  const statusEl = document.getElementById(`subRowStatus_${idx}`);
  if (statusEl) {
    statusEl.style.display = 'block';
    statusEl.style.color = 'var(--text-secondary)';
    statusEl.textContent = '正在通过 SSH 长连接握手并探测 /proc 资源...';
  }

  const srvId = editingServerId || 'srv_temp';
  const res = await callApi('test_subsystem_ssh', srvId, sub.host, parseInt(sub.port || 22, 10), sub.username, sub.password);
  if (res && res.success) {
    if (statusEl) {
      statusEl.style.color = 'var(--system-green)';
      statusEl.textContent = `✔ ${res.message}`;
    }
    showToast(res.message, 'success');
  } else {
    if (statusEl) {
      statusEl.style.color = 'var(--system-red)';
      statusEl.textContent = `✖ ${res?.message || res?.error || '连接失败'}`;
    }
    showToast(res?.message || 'SSH 连通性测试未通过', 'error');
  }
};

// Helper functions for modern Modal dialogs
function openHardwareNodeModal() {
  const modal = document.getElementById('hardwareNodeModalBackdrop');
  if (modal) {
    modal.style.display = 'flex';
    modal.classList.add('active');
  }
}
function closeHardwareNodeModal() {
  const modal = document.getElementById('hardwareNodeModalBackdrop');
  if (modal) {
    modal.style.display = 'none';
    modal.classList.remove('active');
  }
}
function openSystemServerModal() {
  const modal = document.getElementById('systemServerModalBackdrop');
  if (modal) {
    modal.style.display = 'flex';
    modal.classList.add('active');
  }
}
function closeSystemServerModal() {
  const modal = document.getElementById('systemServerModalBackdrop');
  if (modal) {
    modal.style.display = 'none';
    modal.classList.remove('active');
  }
}

// Global ESC key listener to dismiss all modern modals
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeHardwareNodeModal();
    closeSystemServerModal();
    const detailModal = document.getElementById('serverDetailModalBackdrop');
    if (detailModal) detailModal.style.display = 'none';
  }
});

function renderServerManagementList() {
  const container = document.getElementById('serversManagementContainer');
  const sysContainer = document.getElementById('systemServersManagementContainer');
  const nodesBadge = document.getElementById('clusterHardwareNodesCountBadge') || document.getElementById('nodesCountBadge');
  const sysBadge = document.getElementById('sysServersCountBadge');

  const filterQuery = (document.getElementById('clusterSearchInput')?.value || '').trim().toLowerCase();
  const isRowView = state.clusterViewMode === 'row';

  // Apply layout class to both containers
  if (container) {
    container.className = isRowView ? 'servers-management-grid servers-row-layout' : 'servers-management-grid';
  }
  if (sysContainer) {
    sysContainer.className = isRowView ? 'servers-management-grid servers-row-layout' : 'servers-management-grid';
  }

  // 1. Render Hardware Nodes with Search Filtering
  if (container) {
    let filteredNodes = state.servers;
    if (filterQuery) {
      filteredNodes = state.servers.filter(srv => {
        const name = (srv.name || '').toLowerCase();
        const ip = (srv.ip || '').toLowerCase();
        const model = (srv.model || '').toLowerCase();
        const serial = (srv.serial || '').toLowerCase();
        const brand = (srv.brand || '').toLowerCase();
        return name.includes(filterQuery) || ip.includes(filterQuery) || model.includes(filterQuery) || serial.includes(filterQuery) || brand.includes(filterQuery);
      });
    }

    if (nodesBadge) nodesBadge.textContent = `${filteredNodes.length} / ${state.servers.length} 个节点`;

    if (filteredNodes.length === 0) {
      container.innerHTML = `
        <div style="grid-column: 1 / -1; padding: 20px; text-align: center; color: var(--text-tertiary); background:var(--surface-secondary); border-radius:8px; font-size:12px;">
          ${filterQuery ? `未找到匹配「${filterQuery}」的硬件节点` : '暂无硬件节点'}
        </div>
      `;
    } else {
      container.innerHTML = filteredNodes.map(srv => {
        const telemetry = state.cluster_telemetry.find(t => t.id === srv.id) || {};
        const isConn = telemetry.connected;
        const errMsg = telemetry.error_msg || (!isConn ? '网络连接超时或 BMC 未响应 (请检查网络/密码/Administrator权限)' : '');
        const latencyText = isConn ? (telemetry.latency_ms ? `${telemetry.latency_ms}ms` : '<10ms') : '断开';

        const brandDisplayMap = {
          'dell': 'Dell',
          'inspur': '浪潮 Inspur',
          'huawei': '华为 Huawei',
          'supermicro': '超微 Supermicro',
          'lenovo': '联想 Lenovo',
          'generic': 'IPMI'
        };
        const brandBadgeText = brandDisplayMap[srv.brand] || (srv.brand ? srv.brand.toUpperCase() : 'Dell');

        return `
          <div class="server-item-card" style="border-left: 3px solid var(--system-blue);">
            <div class="server-item-header">
              <div class="server-name-badge">
                <span class="status-dot" style="background:${isConn ? 'var(--system-green)' : 'var(--system-red)'}"></span>
                <div style="display:flex; flex-direction:column; align-items:flex-start; gap:1px; min-width:0;">
                  <div style="display:flex; align-items:center; gap:4px;">
                    <span class="micro-capsule capsule-blue" style="font-size:8px; padding:0 4px; line-height:11px;">${brandBadgeText}</span>
                  </div>
                  <span class="server-name-text" title="${srv.name}">${srv.name}</span>
                </div>
              </div>
              <span class="badge ${isConn ? 'badge-normal' : 'badge-warning'}" style="font-size:9.5px; padding:1px 5px; cursor:help;" title="${isConn ? '节点已成功建立 IPMI RMCP+ 会话并受控' : `⚠️ 离线详情: ${errMsg}`}">${isConn ? '已联机' : '离线/未连'}</span>
            </div>

            <div class="server-ip-model" style="display:flex; flex-direction:column; gap:2px;">
              <div>${srv.model || '通用服务器'} | BMC: ${srv.ip} | 延时: ${latencyText} | 账户: ${srv.user}</div>
              ${srv.serial ? `<div><span class="micro-capsule capsule-indigo" style="font-size:8.5px; padding:0 4px; line-height:13px; font-weight:600;" title="出厂资产序列号 (Service Tag / SN)">SN: ${srv.serial}</span></div>` : ''}
            </div>

            <div class="server-card-actions">
              <button class="secondary-btn" onclick="testServerPing('${srv.id}')" title="断开该节点当前所有连接并重新尝试握手与采样">连接</button>
              <button class="secondary-btn" onclick="editServerModal('${srv.id}')">配置</button>
              <button class="primary-btn" onclick="openHardwareNodeDetailModal('${srv.id}')">详情</button>
              ${state.servers.length > 1 ? `<button class="secondary-btn" style="color:var(--system-red);" onclick="deleteServerItem('${srv.id}')">删除</button>` : ''}
            </div>
          </div>
        `;
      }).join('');
    }
  }

  // 2. Render System Servers with Search Filtering
  if (sysContainer) {
    let filteredSys = state.system_servers;
    if (filterQuery) {
      filteredSys = state.system_servers.filter(sys => {
        const name = (sys.name || '').toLowerCase();
        const host = (sys.host || '').toLowerCase();
        const os = (sys.os_name || '').toLowerCase();
        return name.includes(filterQuery) || host.includes(filterQuery) || os.includes(filterQuery);
      });
    }

    if (sysBadge) sysBadge.textContent = `${filteredSys.length} / ${state.system_servers.length} 个服务器`;

    if (filteredSys.length === 0) {
      sysContainer.innerHTML = `
        <div style="grid-column: 1 / -1; padding: 20px; text-align: center; color: var(--text-tertiary); background:var(--surface-secondary); border-radius:8px; font-size:12px;">
          ${filterQuery ? `未找到匹配「${filterQuery}」的系统服务器` : '暂无系统服务器，请点击右上角「+ 添加系统服务器」'}
        </div>
      `;
    } else {
      sysContainer.innerHTML = filteredSys.map(sys => {
        const isConn = Boolean(sys && sys.connected === true);
        const boundNode = state.servers.find(n => n.id === sys.node_id);
        const boundName = boundNode ? boundNode.name : '独立服务器';
        const latencyText = isConn ? (sys.latency_ms ? `${sys.latency_ms}ms` : '<10ms') : '断开';
        const sysErrMsg = sys.last_error || (!isConn ? 'SSH 握手或鉴权超时，请检查端口、密码与密钥' : '');

        return `
          <div class="server-item-card" style="border-left: 3px solid var(--system-green);">
            <div class="server-item-header">
              <div class="server-name-badge">
                <span class="status-dot" style="background:${isConn ? 'var(--system-green)' : 'var(--system-red)'}"></span>
                <div style="display:flex; flex-direction:column; align-items:flex-start; gap:1px; min-width:0;">
                  <span class="micro-capsule capsule-cyan" style="font-size:8px; padding:0 4px; line-height:11px;">SSH</span>
                  <span class="server-name-text" title="${sys.name}">${sys.name}</span>
                </div>
              </div>
              <span class="badge ${isConn ? 'badge-normal' : 'badge-warning'}" style="font-size:9.5px; padding:1px 5px; cursor:help;" title="${isConn ? 'SSH 探针长连接稳定巡检中' : `⚠️ 离线详情: ${sysErrMsg}`}">${isConn ? '已联机' : '离线/未连'}</span>
            </div>

            <div class="server-ip-model">
              SSH: ${sys.username}@${sys.host}:${sys.port || 22}${sys.os_name ? ` · ${sys.os_name}` : ''} | 延时: ${latencyText} | 宿主: ${boundNode ? `<strong style="color:var(--text-primary);">${boundName}</strong>` : `<span style="color:var(--text-tertiary);">独立无绑定</span>`} | 账户: ${sys.username}
            </div>

            <div class="server-card-actions">
              <button class="secondary-btn" onclick="testSystemServerPing('${sys.id}')" title="断开该服务器当前会话并重新发起 SSH 探测">连接</button>
              <button class="secondary-btn" onclick="editSystemServerModal('${sys.id}')">配置</button>
              <button class="primary-btn" onclick="openServerDetailModal('${sys.id}')">详情</button>
              <button class="secondary-btn" style="color:var(--system-red);" onclick="deleteSystemServerItem('${sys.id}')">删除</button>
            </div>
          </div>
        `;
      }).join('');
    }
  }
}

let editingSysServerId = null;

window.editSystemServerModal = function(sysId) {
  const sys = state.system_servers.find(s => s.id === sysId);
  if (!sys) return;
  editingSysServerId = sysId;

  document.getElementById('sysServerFormTitle').textContent = `编辑系统服务器: ${sys.name}`;
  document.getElementById('sysSrvFormName').value = sys.name || '';
  document.getElementById('sysSrvFormHost').value = sys.host || '';
  document.getElementById('sysSrvFormPort').value = sys.port || 22;
  document.getElementById('sysSrvFormUser').value = sys.username || 'root';
  document.getElementById('sysSrvFormPassword').value = sys.password || '';
  if (document.getElementById('sysSrvFormOsName')) {
    document.getElementById('sysSrvFormOsName').value = sys.os_name || '';
  }

  // Populate node binding options
  const nodeSel = document.getElementById('sysSrvFormNodeSelect');
  if (nodeSel) {
    let opts = '<option value="">-- 独立服务器 (不绑定任何硬件节点) --</option>';
    state.servers.forEach(n => {
      opts += `<option value="${n.id}" ${n.id === sys.node_id ? 'selected' : ''}>🖥️ 绑定到: ${n.name} (${n.ip})</option>`;
    });
    nodeSel.innerHTML = opts;
  }

  const testResBox = document.getElementById('sysSrvTestResultBox');
  if (testResBox) testResBox.style.display = 'none';
  openSystemServerModal();
};

window.deleteSystemServerItem = async function(sysId) {
  if (confirm('确定要移除此系统服务器配置吗？')) {
    const res = await callApi('delete_system_server', sysId);
    if (res && res.success) {
      showToast(res.message, 'success');
      await refreshAllData();
      renderServerManagementList();
    } else {
      showToast(res?.message || '删除失败', 'error');
    }
  }
};

window.handleChassisPower = async function(srvId, action) {
  const srv = (state.servers || []).find(s => s.id === srvId);
  const srvName = srv ? srv.name : '该服务器';

  const actionTextMap = {
    'on': '开机 (Power On)',
    'soft': 'ACPI 关机 (Soft Shutdown)',
    'reset': '强制重启 (Chassis Reset)',
    'off': '⚠️ 强制断电 (硬关机)'
  };
  const text = actionTextMap[action] || action;

  if (action === 'off') {
    if (!confirm(`【高危操作警告】确定要向「${srvName}」下发强制断电指令吗？\n\n该操作将直接切断物理机供电（硬关机），未保存的数据将丢失！`)) {
      return;
    }
  } else if (action === 'reset') {
    if (!confirm(`确定要重启服务器「${srvName}」吗？\n\n将向 BMC 发送硬件跳线 Reset 信号。`)) {
      return;
    }
  } else if (action === 'soft') {
    if (!confirm(`确定要向「${srvName}」发送 ACPI 正常关机信号吗？\n\n操作系统将尝试保存数据并退出。`)) {
      return;
    }
  } else if (action === 'on') {
    if (!confirm(`确定要向「${srvName}」下发 IPMI 远程开机指令吗？`)) {
      return;
    }
  }

  showToast(`正在向 [${srvName}] 发送 IPMI ${text} 指令...`, 'info');
  try {
    const res = await callApi('control_server_power', srvId, action);
    if (res && res.success) {
      showToast(res.message || `[${srvName}] IPMI ${text} 执行成功`, 'success');
      // 延迟 1.5 秒自动重测并更新当前服务器在线状态
      setTimeout(async () => {
        await refreshAllData();
      }, 1500);
    } else {
      showToast(res?.error || `[${srvName}] IPMI ${text} 执行失败`, 'error');
    }
  } catch (err) {
    showToast(`IPMI 电源控制异常: ${err.message || err}`, 'error');
  }
};

window.testServerPing = async function(srvId) {
  showToast('正在中断旧连接并重新对该物理节点发起连接...', 'info');
  const res = await callApi('force_reconnect', srvId);
  if (res && res.success) {
    showToast('已重新发起连接握手与数据同步', 'success');
  } else {
    showToast(res?.message || '重新连接失败', 'error');
  }
  await refreshAllData();
  renderServerManagementList();
};

window.testSystemServerPing = async function(sysId) {
  const sys = state.system_servers.find(s => s.id === sysId);
  if (!sys) return;
  showToast(`正在中断旧会话并对服务器「${sys.name}」(${sys.host}:${sys.port || 22}) 重新发起连接...`, 'info');
  const res = await callApi('reconnect_system_server', sysId);
  if (res && res.success) {
    showToast(res.message || '已重新发起 SSH 连接握手', 'success');
  } else {
    showToast(res?.message || res?.error || '连接失败: 无法建立网络连接', 'error');
  }
  await refreshAllData();
  renderServerManagementList();
};

window.editServerModal = function(srvId) {
  const srv = state.servers.find(s => s.id === srvId);
  if (!srv) return;
  editingServerId = srvId;
  state.editing_server_subsystems = JSON.parse(JSON.stringify(srv.subsystems || []));
  renderServerFormSubsystems();

  document.getElementById('serverFormTitle').textContent = `编辑节点: ${srv.name}`;
  document.getElementById('srvFormName').value = srv.name;
  const brandSel = document.getElementById('srvFormBrand');
  if (brandSel) brandSel.value = srv.brand || 'dell';
  document.getElementById('srvFormModel').value = srv.model || '';
  const serialEl = document.getElementById('srvFormSerial');
  if (serialEl) serialEl.value = srv.serial || '';
  document.getElementById('srvFormIp').value = srv.ip;
  document.getElementById('srvFormUser').value = srv.user;
  document.getElementById('srvFormPassword').value = srv.password;

  openHardwareNodeModal();
};

window.deleteServerItem = async function(srvId) {
  if (confirm('确定要从集群中移除此服务器配置吗？')) {
    const res = await callApi('delete_server', srvId);
    if (res && res.success) {
      showToast(res.message, 'success');
      await refreshAllData();
      renderServerManagementList();
    } else {
      showToast(res?.message || '删除失败', 'error');
    }
  }
};

// ==========================================
// Alert Center Tab
// ==========================================
function initAlertCenterTab() {
  const btnTestAudio = document.getElementById('btnTestAlertAudio');
  if (btnTestAudio) {
    btnTestAudio.addEventListener('click', async () => {
      const soundType = document.getElementById('alertSoundType').value;
      const customPath = document.getElementById('alertCustomSoundPath').value;
      showToast('正在触发告警音频试听...', 'info');
      await callApi('test_alert_sound', soundType, customPath);
    });
  }

  const btnTestTTS = document.getElementById('btnTestAlertTTS');
  if (btnTestTTS) {
    btnTestTTS.addEventListener('click', async () => {
      showToast('正在触发语音合成朗读播报...', 'info');
      await callApi('test_alert_tts', '云枢系统告警测试！服务器硬件及系统指标运行正常。');
    });
  }

  // 外部 Webhook 实时测试推送 (移植自 Apprise & ANotify)
  const btnTestWebhook = document.getElementById('btnTestWebhookNotify');
  if (btnTestWebhook) {
    btnTestWebhook.addEventListener('click', async () => {
      const cfgOverride = {
        webhook_enabled: true,
        webhook_channel: document.getElementById('alertWebhookChannel').value,
        webhook_url: document.getElementById('alertWebhookUrl').value.trim(),
        webhook_secret: document.getElementById('alertWebhookSecret').value.trim()
      };
      if (!cfgOverride.webhook_url && !['serverchan', 'pushplus', 'bark'].includes(cfgOverride.webhook_channel)) {
        showToast('请先输入有效的 Webhook 接口 URL 或 Token', 'warning');
        return;
      }
      showToast('正在向所选通道发送测试推送...', 'info');
      const res = await callApi('test_alert_webhook', cfgOverride);
      if (res && res.success) {
        showToast(`已成功向 [${res.channel}] 发送测试通知！`, 'success');
      } else {
        showToast(`推送失败: ${res?.error || '请求超时或接口拒绝'}`, 'error');
      }
    });
  }

  // 通道切换时动态修改提示文本
  const selWebhookChan = document.getElementById('alertWebhookChannel');
  if (selWebhookChan) {
    selWebhookChan.addEventListener('change', (e) => {
      const ch = e.target.value;
      const descEl = document.getElementById('alertWebhookUrlDesc');
      const urlInput = document.getElementById('alertWebhookUrl');
      const secretInput = document.getElementById('alertWebhookSecret');
      if (ch === 'serverchan') {
        if (descEl) descEl.textContent = '可填 SendKey (如 SCTxxxx) 或完整接口 https://sctapi.ftqq.com/<SendKey>.send';
        if (urlInput) urlInput.placeholder = 'SCT... 或完整接口';
      } else if (ch === 'dingtalk') {
        if (descEl) descEl.textContent = '钉钉自定义机器人 Webhook (https://oapi.dingtalk.com/robot/send?access_token=...)';
        if (urlInput) urlInput.placeholder = 'https://oapi.dingtalk.com/robot/send?access_token=...';
      } else if (ch === 'feishu') {
        if (descEl) descEl.textContent = '飞书群自定义机器人 Webhook (https://open.feishu.cn/open-apis/bot/v2/hook/...)';
        if (urlInput) urlInput.placeholder = 'https://open.feishu.cn/open-apis/bot/v2/hook/...';
      } else if (ch === 'wechat_work') {
        if (descEl) descEl.textContent = '企业微信群机器人 Webhook (https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...)';
        if (urlInput) urlInput.placeholder = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...';
      } else if (ch === 'bark') {
        if (descEl) descEl.textContent = 'Bark 服务器地址或 DeviceKey (如 https://api.day.app/your_key)';
        if (urlInput) urlInput.placeholder = 'https://api.day.app/your_key 或 直接填 Key';
      } else if (ch === 'pushplus') {
        if (descEl) descEl.textContent = 'PushPlus 推送 Token (可填在下方密钥栏或此处)';
        if (urlInput) urlInput.placeholder = 'PushPlus Token';
      } else {
        if (descEl) descEl.textContent = '标准 HTTP POST JSON 接口 (接收 title, content, timestamp 字段)';
        if (urlInput) urlInput.placeholder = 'https://...';
      }
    });
  }

  const btnSaveAlert = document.getElementById('btnSaveAlertConfig');
  if (btnSaveAlert) {
    btnSaveAlert.addEventListener('click', async () => {
      const cfg = {
        enabled: document.getElementById('alertGlobalEnabled').checked,
        sound_enabled: document.getElementById('alertSoundEnabled').checked,
        sound_type: document.getElementById('alertSoundType').value,
        custom_sound_path: document.getElementById('alertCustomSoundPath').value.trim(),
        tts_enabled: document.getElementById('alertTtsEnabled').checked,
        cooldown_sec: parseInt(document.getElementById('alertCooldownSec').value || 60, 10),
        // 外部 Webhook 消息通知
        webhook_enabled: document.getElementById('alertWebhookEnabled').checked,
        webhook_channel: document.getElementById('alertWebhookChannel').value,
        webhook_url: document.getElementById('alertWebhookUrl').value.trim(),
        webhook_secret: document.getElementById('alertWebhookSecret').value.trim(),
        // 自定义告警内容模版
        custom_alert_title_template: document.getElementById('alertTitleTemplate').value.trim(),
        custom_alert_body_template: document.getElementById('alertBodyTemplate').value.trim(),
        // 板块 A: 硬件节点
        node_cpu_temp_enabled: document.getElementById('enableNodeCpuTemp').checked,
        node_cpu_temp_threshold: parseFloat(document.getElementById('threshNodeCpuTemp').value || 80),
        // 板块 B: 系统服务器
        server_cpu_enabled: document.getElementById('enableServerCpu').checked,
        server_cpu_threshold: parseFloat(document.getElementById('threshServerCpu').value || 90),
        server_mem_enabled: document.getElementById('enableServerMem').checked,
        server_mem_threshold: parseFloat(document.getElementById('threshServerMem').value || 90),
        server_swap_enabled: document.getElementById('enableServerSwap').checked,
        server_swap_threshold: parseFloat(document.getElementById('threshServerSwap').value || 80),
        server_disk_enabled: document.getElementById('enableServerDisk').checked,
        server_disk_threshold: parseFloat(document.getElementById('threshServerDisk').value || 92)
      };

      const res = await callApi('save_alert_config', cfg);
      if (res && res.success) {
        showToast('保存成功', 'success');
        state.alert_config = cfg;
        if (state.config) {
          state.config.alert = { ...cfg };
        }
      } else {
        showToast(res?.message || '保存失败', 'error');
      }
    });
  }

  const selSoundType = document.getElementById('alertSoundType');
  if (selSoundType) {
    selSoundType.addEventListener('change', (e) => {
      const isCustom = e.target.value === 'custom';
      document.getElementById('rowCustomSoundPath').style.display = isCustom ? 'flex' : 'none';
    });
  }

  const btnClearLog = document.getElementById('btnClearAlertHistory');
  if (btnClearLog) {
    btnClearLog.addEventListener('click', async () => {
      await callApi('clear_alert_history');
      state.alert_history = [];
      renderAlertsCenter();
      showToast('历史告警日志已清空', 'success');
    });
  }
}

function renderAlertsCenter() {
  const cfg = state.alert_config || {};

  // Fill in form values
  if (cfg.enabled !== undefined) document.getElementById('alertGlobalEnabled').checked = !!cfg.enabled;
  if (cfg.sound_enabled !== undefined) document.getElementById('alertSoundEnabled').checked = !!cfg.sound_enabled;
  if (cfg.sound_type) {
    document.getElementById('alertSoundType').value = cfg.sound_type;
    document.getElementById('rowCustomSoundPath').style.display = cfg.sound_type === 'custom' ? 'flex' : 'none';
  }
  if (cfg.custom_sound_path !== undefined) document.getElementById('alertCustomSoundPath').value = cfg.custom_sound_path;
  if (cfg.tts_enabled !== undefined) document.getElementById('alertTtsEnabled').checked = !!cfg.tts_enabled;
  if (cfg.cooldown_sec !== undefined) document.getElementById('alertCooldownSec').value = cfg.cooldown_sec;

  // 外部 Webhook 推送字段回显
  if (cfg.webhook_enabled !== undefined) document.getElementById('alertWebhookEnabled').checked = !!cfg.webhook_enabled;
  if (cfg.webhook_channel) document.getElementById('alertWebhookChannel').value = cfg.webhook_channel;
  if (cfg.webhook_url !== undefined) document.getElementById('alertWebhookUrl').value = cfg.webhook_url;
  if (cfg.webhook_secret !== undefined) document.getElementById('alertWebhookSecret').value = cfg.webhook_secret;

  // 自定义模版回显
  if (cfg.custom_alert_title_template !== undefined) document.getElementById('alertTitleTemplate').value = cfg.custom_alert_title_template;
  if (cfg.custom_alert_body_template !== undefined) document.getElementById('alertBodyTemplate').value = cfg.custom_alert_body_template;

  // 板块 A: 硬件节点
  if (cfg.node_cpu_temp_enabled !== undefined) document.getElementById('enableNodeCpuTemp').checked = !!cfg.node_cpu_temp_enabled;
  if (cfg.node_cpu_temp_threshold !== undefined) document.getElementById('threshNodeCpuTemp').value = cfg.node_cpu_temp_threshold;

  // 板块 B: 系统服务器
  if (cfg.server_cpu_enabled !== undefined) document.getElementById('enableServerCpu').checked = !!cfg.server_cpu_enabled;
  if (cfg.server_cpu_threshold !== undefined) document.getElementById('threshServerCpu').value = cfg.server_cpu_threshold;
  if (cfg.server_mem_enabled !== undefined) document.getElementById('enableServerMem').checked = !!cfg.server_mem_enabled;
  if (cfg.server_mem_threshold !== undefined) document.getElementById('threshServerMem').value = cfg.server_mem_threshold;
  if (cfg.server_swap_enabled !== undefined) document.getElementById('enableServerSwap').checked = !!cfg.server_swap_enabled;
  if (cfg.server_swap_threshold !== undefined) document.getElementById('threshServerSwap').value = cfg.server_swap_threshold;
  if (cfg.server_disk_enabled !== undefined) document.getElementById('enableServerDisk').checked = !!cfg.server_disk_enabled;
  if (cfg.server_disk_threshold !== undefined) document.getElementById('threshServerDisk').value = cfg.server_disk_threshold;

  // Render History Table
  const tbody = document.getElementById('alertHistoryTableBody');
  if (!tbody) return;

  const history = state.alert_history || [];
  if (history.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="5" style="text-align:center; padding:24px; color:var(--text-tertiary);">暂无告警记录，系统运行平稳健康</td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = history.map(item => `
    <tr class="${item.level === 'danger' ? 'alert-row-danger' : 'alert-row-warning'}">
      <td style="font-family:var(--font-mono); font-size:11.5px;">${item.time}</td>
      <td style="font-weight:600;">${item.source}</td>
      <td>
        <span class="badge ${item.level === 'danger' ? 'badge-danger' : 'badge-warning'}">${item.title}</span>
      </td>
      <td style="color:var(--text-secondary); font-size:12px;">${item.message}</td>
      <td>
        <span class="badge ${item.level === 'danger' ? 'badge-danger' : 'badge-warning'}">${item.level.toUpperCase()}</span>
      </td>
    </tr>
  `).join('');
}

// ==========================================
// ==========================================
// System Logs Tab Interactive Logic
// ==========================================
function initSystemLogsTab() {
  const btnToggleDebug = document.getElementById('btnToggleLogDebug');
  const btnRefreshLogs = document.getElementById('btnRefreshLogs');
  const btnClearLogs = document.getElementById('btnClearLogView');

  if (btnToggleDebug) {
    btnToggleDebug.addEventListener('change', async (e) => {
      const enabled = e.target.checked;
      showToast(`正在切换日志模式: ${enabled ? '全量调试模式' : '标准报错模式'}...`, 'info');
      const res = await callApi('set_log_debug_mode', enabled);
      if (res && res.success) {
        showToast(res.message, 'success');
        updateLogViewUI(enabled);
        await refreshSystemLogs(true);
      }
    });
  }

  if (btnRefreshLogs) {
    btnRefreshLogs.addEventListener('click', async () => {
      await refreshSystemLogs(true);
      showToast('日志已刷新', 'info');
    });
  }

  if (btnClearLogs) {
    btnClearLogs.addEventListener('click', async () => {
      await callApi('clear_system_logs');
      const streamBox = document.getElementById('systemLogStreamBox');
      if (streamBox) {
        streamBox.innerHTML = '<div style="color:#8b949e; text-align:center; padding:60px 0;">日志视图已清空</div>';
      }
      const countEl = document.getElementById('logTotalCountText');
      if (countEl) countEl.textContent = '0 条记录';
      showToast('已清空当前实时日志视图', 'success');
    });
  }
}

function updateLogViewUI(debugEnabled) {
  const badge = document.getElementById('logModeBadge');
  const ruleText = document.getElementById('logFilterRuleText');
  const chk = document.getElementById('btnToggleLogDebug');
  if (chk) chk.checked = debugEnabled;

  if (badge) {
    badge.className = `badge ${debugEnabled ? 'badge-danger' : 'badge-normal'}`;
    badge.textContent = debugEnabled ? '🐞 调试模式 (全量详细日志)' : '标准模式 (仅实时报错)';
  }
  if (ruleText) {
    ruleText.style.color = debugEnabled ? '#a371f7' : '#f0883e';
    ruleText.textContent = debugEnabled ? '重要业务/操作事件 + 全部异常报错 (已自动过滤无异常轮询底噪)' : '仅 ERROR / WARNING / 异常报错';
  }
}

async function refreshSystemLogs(forceScroll = false) {
  const streamBox = document.getElementById('systemLogStreamBox');
  if (!streamBox) return;

  const res = await callApi('get_system_logs', 400);
  if (!res || !res.success) return;

  updateLogViewUI(!!res.debug_mode);

  const logs = res.logs || [];
  const countEl = document.getElementById('logTotalCountText');
  if (countEl) countEl.textContent = `${logs.length} 条记录`;

  if (logs.length === 0) {
    streamBox.innerHTML = `
      <div style="color:#8b949e; text-align:center; padding:60px 0;">
        ${res.debug_mode ? '暂无调试日志记录' : '暂无错误异常日志。系统运行健康良好！'}
      </div>
    `;
    return;
  }

  // 格式化输出终端风格日志行
  const levelColors = {
    'ERROR': '#ff7b72',
    'CRITICAL': '#f85149',
    'WARNING': '#d29922',
    'INFO': '#58a6ff',
    'DEBUG': '#8b949e'
  };

  const linesHtml = logs.map(l => {
    const col = levelColors[l.level] || '#c9d1d9';
    return `
      <div style="display:flex; align-items:flex-start; gap:8px; padding:2px 0; border-bottom:1px solid rgba(255,255,255,0.03);">
        <span style="color:#8b949e; font-size:11px; flex-shrink:0;">${l.time_short || l.timestamp}</span>
        <span style="color:${col}; font-weight:700; font-size:10.5px; padding:0 4px; border-radius:3px; background:rgba(255,255,255,0.06); flex-shrink:0;">${l.level}</span>
        <span style="color:#79c0ff; flex-shrink:0;">[${l.logger}]</span>
        <span style="color:#e6edf3; flex:1;">${escapeHtml(l.message)}</span>
      </div>
    `;
  }).join('');

  const isScrolledToBottom = streamBox.scrollHeight - streamBox.clientHeight <= streamBox.scrollTop + 50;
  streamBox.innerHTML = linesHtml;

  if (forceScroll || isScrolledToBottom) {
    streamBox.scrollTop = streamBox.scrollHeight;
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Requirement 2: Settings & Preferences Tab (1s refresh & Autostart)
// ==========================================
function populatePreferencesForm() {
  if (!state.config) return;
  const cfg = state.config.ipmi || {};
  const logCfg = state.config.logging || {};

  const prefUiRateEl = document.getElementById('prefUiRefreshRate');
  if (prefUiRateEl) prefUiRateEl.value = cfg.ui_refresh_sec || state.uiRefreshSec || 1;

  const prefGlobalSensorEl = document.getElementById('prefGlobalSensorRate');
  if (prefGlobalSensorEl) prefGlobalSensorEl.value = cfg.global_sensor_poll_sec || 60;

  const prefNodePingInt = document.getElementById('prefNodePingInterval');
  if (prefNodePingInt) prefNodePingInt.value = cfg.node_ping_interval_sec || 10;
  const prefNodePingRt = document.getElementById('prefNodePingRetry');
  if (prefNodePingRt) prefNodePingRt.value = cfg.node_ping_retry_count || 2;

  const chkOfflineReconn = document.getElementById('chkOfflineReconnect');
  const prefOfflineReconnInt = document.getElementById('prefOfflineReconnectInterval');
  const rowOfflineReconnInt = document.getElementById('rowOfflineReconnectInterval');
  const isOfflineReconnOn = (cfg.offline_reconnect_enabled ?? '1') === '1';
  if (chkOfflineReconn) {
    chkOfflineReconn.checked = isOfflineReconnOn;
    if (rowOfflineReconnInt) rowOfflineReconnInt.style.display = isOfflineReconnOn ? 'flex' : 'none';
  }
  if (prefOfflineReconnInt) prefOfflineReconnInt.value = cfg.offline_reconnect_interval_sec || 15;

  const prefServerPingInt = document.getElementById('prefServerPingInterval');
  if (prefServerPingInt) prefServerPingInt.value = cfg.server_ping_interval_sec || 5;
  const prefServerPingRt = document.getElementById('prefServerPingRetry');
  if (prefServerPingRt) prefServerPingRt.value = cfg.server_ping_retry_count || 2;

  const pollModeSel = document.getElementById('prefIpmiPollMode');
  const customRow = document.getElementById('rowCustomRefreshRate');
  const savedPollMode = cfg.ipmi_poll_mode || 'auto_1s';
  if (pollModeSel) {
    pollModeSel.value = savedPollMode;
    if (customRow) customRow.style.display = savedPollMode === 'custom' ? 'flex' : 'none';
  }

  const prefRefreshRate = document.getElementById('prefRefreshRate');
  if (prefRefreshRate) prefRefreshRate.value = cfg.auto_refresh_sec || state.autoRefreshSec || 3;
  const prefDefaultView = document.getElementById('prefDefaultView');
  if (prefDefaultView) prefDefaultView.value = cfg.dashboard_view_mode || state.dashboardViewMode || 'probe';
  const prefSubPoll = document.getElementById('prefSubsystemPollRate');
  if (prefSubPoll) prefSubPoll.value = cfg.subsystem_poll_sec || state.subsystem_poll_sec || 2;
  const prefIpmiTo = document.getElementById('prefIpmiTimeout');
  if (prefIpmiTo) prefIpmiTo.value = cfg.default_timeout_sec || 30;
  const prefIpmiRt = document.getElementById('prefIpmiRetry');
  if (prefIpmiRt) prefIpmiRt.value = cfg.default_retry_count || 2;

  const prefLogRet = document.getElementById('prefLogRetentionDays');
  if (prefLogRet) prefLogRet.value = logCfg.log_retention_days || 7;

  // IPMI 极速优化项回显 (浓缩为三大协同组合，默认均为开启 1)
  const chkDcmi = document.getElementById('chkOptDcmiTemp');
  if (chkDcmi) chkDcmi.checked = (cfg.opt_dcmi_temp_enabled ?? '1') === '1';

  const chkCipher = document.getElementById('chkOptCipherSuite');
  if (chkCipher) chkCipher.checked = (cfg.opt_cipher_suite_enabled ?? '1') === '1';

  const chkKeepalive = document.getElementById('chkOptKeepaliveSession');
  if (chkKeepalive) chkKeepalive.checked = (cfg.opt_keepalive_session_enabled ?? '1') === '1';
}

function initSettingsTab() {
  const chkAutostart = document.getElementById('chkAutoStart');
  const autostartTip = document.getElementById('autostartStatusTip');
  const prefRefreshRate = document.getElementById('prefRefreshRate');
  const prefDefaultView = document.getElementById('prefDefaultView');

  // Load preferences values
  populatePreferencesForm();

  const chkOfflineReconn = document.getElementById('chkOfflineReconnect');
  const rowOfflineReconnInt = document.getElementById('rowOfflineReconnectInterval');
  if (chkOfflineReconn) {
    chkOfflineReconn.addEventListener('change', (e) => {
      if (rowOfflineReconnInt) rowOfflineReconnInt.style.display = e.target.checked ? 'flex' : 'none';
    });
  }

  const pollModeSel = document.getElementById('prefIpmiPollMode');
  const customRow = document.getElementById('rowCustomRefreshRate');
  if (pollModeSel) {
    pollModeSel.addEventListener('change', (e) => {
      if (customRow) customRow.style.display = e.target.value === 'custom' ? 'flex' : 'none';
    });
  }

  // 日志保存期限回显
  const prefLogRet = document.getElementById('prefLogRetentionDays');
  if (prefLogRet) prefLogRet.value = state.config?.logging?.log_retention_days || 7;

  chkAutostart.addEventListener('change', async (e) => {
    const isChecked = e.target.checked;
    const res = await callApi('set_autostart', isChecked);
    if (res && res.success) {
      showToast(res.message, 'success');
      updateAutostartTipUI(isChecked);
    } else {
      showToast(res?.message || '开机自启更新失败', 'error');
      chkAutostart.checked = !isChecked;
    }
  });

  document.getElementById('btnSavePreferences').addEventListener('click', async () => {
    const uiSecVal = parseInt(document.getElementById('prefUiRefreshRate')?.value || 1, 10);
    const validUiRate = Math.max(1, Math.min(10, isNaN(uiSecVal) ? 1 : uiSecVal));
    const secVal = parseInt(prefRefreshRate.value, 10);
    const viewVal = prefDefaultView.value;
    const globalSensorVal = parseInt(document.getElementById('prefGlobalSensorRate')?.value || 60, 10);
    const nodePingIntVal = parseInt(document.getElementById('prefNodePingInterval')?.value || 10, 10);
    const nodePingRtVal = parseInt(document.getElementById('prefNodePingRetry')?.value || 2, 10);
    const serverPingIntVal = parseInt(document.getElementById('prefServerPingInterval')?.value || 5, 10);
    const serverPingRtVal = parseInt(document.getElementById('prefServerPingRetry')?.value || 2, 10);
    const subPollVal = serverPingIntVal;
    const ipmiTimeoutVal = parseInt(document.getElementById('prefIpmiTimeout')?.value || 30, 10);
    const ipmiRetryVal = parseInt(document.getElementById('prefIpmiRetry')?.value || 2, 10);
    const logRetDays = parseInt(document.getElementById('prefLogRetentionDays')?.value || 7, 10);
    const offlineReconnOn = document.getElementById('chkOfflineReconnect')?.checked ? '1' : '0';
    const offlineReconnInt = parseInt(document.getElementById('prefOfflineReconnectInterval')?.value || 15, 10);

    const optDcmiVal = document.getElementById('chkOptDcmiTemp')?.checked ? '1' : '0';
    const optSdrVal = optDcmiVal; // 协同组合1：DCMI 与 SDR 本地缓存同开同关
    const optCipherVal = document.getElementById('chkOptCipherSuite')?.checked ? '1' : '0';
    const optFastRtxVal = optCipherVal; // 协同组合2：Cipher 3 与 局域网快速重传同开同关
    const optKeepaliveVal = document.getElementById('chkOptKeepaliveSession')?.checked ? '1' : '0';
    const optTypeFilterVal = optKeepaliveVal; // 协同组合3：会话保持与传感器定向分流同开同关

    if (isNaN(secVal) || secVal < 1 || secVal > 60) {
      showToast('精准采样周期必须为 1 ~ 60 之间的整数秒', 'error');
      return;
    }

    const pollModeVal = document.getElementById('prefIpmiPollMode')?.value || 'auto_1s';

    state.uiRefreshSec = validUiRate;
    state.autoRefreshSec = secVal;
    state.subsystem_poll_sec = subPollVal;
    startStatusPolling();

    await callApi('save_config', {
      ui_refresh_sec: validUiRate.toString(),
      ipmi_poll_mode: pollModeVal,
      auto_refresh_sec: secVal.toString(),
      global_sensor_poll_sec: globalSensorVal.toString(),
      node_ping_interval_sec: nodePingIntVal.toString(),
      node_ping_retry_count: nodePingRtVal.toString(),
      offline_reconnect_enabled: offlineReconnOn,
      offline_reconnect_interval_sec: offlineReconnInt.toString(),
      server_ping_interval_sec: serverPingIntVal.toString(),
      server_ping_retry_count: serverPingRtVal.toString(),
      subsystem_poll_sec: subPollVal.toString(),
      dashboard_view_mode: viewVal,
      default_timeout_sec: ipmiTimeoutVal.toString(),
      default_retry_count: ipmiRetryVal.toString(),
      log_retention_days: logRetDays.toString(),
      opt_dcmi_temp_enabled: optDcmiVal,
      opt_sdr_cache_enabled: optSdrVal,
      opt_cipher_suite_enabled: optCipherVal,
      opt_fast_retransmit_enabled: optFastRtxVal,
      opt_type_filter_enabled: optTypeFilterVal,
      opt_keepalive_session_enabled: optKeepaliveVal
    });
    await callApi('set_log_retention_days', logRetDays);
    await callApi('set_subsystem_poll_sec', subPollVal);

    setDashboardView(viewVal);
    showToast('保存成功', 'success');
  });

  // Query autostart state
  setTimeout(() => {
    chkAutostart.checked = state.autostart_active;
    updateAutostartTipUI(state.autostart_active);
    const pathEl = document.getElementById('cfgIpmitoolPath');
    if (pathEl) pathEl.textContent = 'ipmitool (内置 Cygwin 稳定运行时)';
  }, 300);
}

function updateAutostartTipUI(isActive) {
  const tip = document.getElementById('autostartStatusTip');
  if (tip) {
    if (isActive) {
      tip.innerHTML = '● <strong>已成功注册开机自启动</strong>，启动后将自动激活各节点保存的温控设定';
      tip.style.borderColor = 'rgba(52, 199, 89, 0.4)';
    } else {
      tip.innerHTML = '○ 未启用开机自启 (绿色滑动开关轻点即生效)';
      tip.style.borderColor = 'var(--border-subtle)';
    }
  }
}

// ========================================================
// 批量运维平台 (Batch Ops Platform Logic)
// 结构展开样式: 按 节点-服务器 层级展示 + 独立服务器分组
// ========================================================
function renderOpsPlatform() {
  renderOpsServerTree();
  initOpsEventHandlers();
  updateOpsSelectionBadge();
}

function renderOpsServerTree() {
  const container = document.getElementById('opsServerHierarchyTree');
  if (!container) return;

  const keyword = (state.ops_filter_keyword || '').trim().toLowerCase();
  const allNodes = state.servers || [];
  const allSysServers = state.system_servers || [];

  if (allSysServers.length === 0) {
    container.innerHTML = `
      <div style="padding:24px 10px; text-align:center; color:var(--text-tertiary); font-size:12px;">
        尚未配置任何系统服务器。<br>请前往「集群管理」添加 Linux SSH 主机。
      </div>
    `;
    return;
  }

  let html = '';

  // 1. Group servers by node
  allNodes.forEach(node => {
    let bound = allSysServers.filter(s => s.node_id === node.id);
    if (keyword) {
      bound = bound.filter(s => 
        (s.name && s.name.toLowerCase().includes(keyword)) ||
        (s.host && s.host.toLowerCase().includes(keyword)) ||
        (node.name && node.name.toLowerCase().includes(keyword)) ||
        (node.ip && node.ip.toLowerCase().includes(keyword))
      );
    }

    if (bound.length === 0 && keyword && !(node.name.toLowerCase().includes(keyword) || node.ip.toLowerCase().includes(keyword))) {
      return;
    }

    const boundIds = bound.map(s => s.id);
    const allChecked = boundIds.length > 0 && boundIds.every(id => state.ops_selected_server_ids.has(id));
    const someChecked = boundIds.some(id => state.ops_selected_server_ids.has(id)) && !allChecked;

    html += `
      <div class="ops-node-group" style="border:1px solid var(--border-subtle); border-radius:8px; overflow:hidden; background:var(--surface-secondary);">
        <!-- Node Group Header -->
        <div style="display:flex; align-items:center; justify-content:space-between; padding:6px 10px; background:rgba(0,0,0,0.03); border-bottom:1px solid var(--border-subtle);">
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer; min-width:0; flex:1;">
            <input type="checkbox" class="probe-checkbox ops-node-checkbox" data-node-id="${node.id}" ${allChecked ? 'checked' : ''} ${boundIds.length === 0 ? 'disabled' : ''} onchange="toggleOpsNodeSelection('${node.id}', this.checked)">
            <span class="micro-capsule capsule-blue" style="font-size:8px; padding:0 4px; line-height:13px;">节点</span>
            <span style="font-size:11.5px; font-weight:600; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${node.name}">${node.name}</span>
          </label>
          <span style="font-size:10px; color:var(--text-tertiary); font-family:var(--font-mono);">${node.ip}</span>
        </div>

        <!-- Bound Servers List (展开样式按节点-服务器依次陈列) -->
        <div style="display:flex; flex-direction:column; padding:4px 6px; gap:3px;">
          ${bound.length === 0 ? `
            <div style="padding:4px 8px; font-size:10.5px; color:var(--text-tertiary);">暂无绑定的系统服务器</div>
          ` : bound.map(srv => {
            const isChecked = state.ops_selected_server_ids.has(srv.id);
            const isConn = srv.connected;
            return `
              <label style="display:flex; align-items:center; justify-content:space-between; padding:4px 8px; border-radius:5px; cursor:pointer; background:${isChecked ? 'rgba(10, 132, 255, 0.08)' : 'var(--surface-primary)'}; border:1px solid ${isChecked ? 'rgba(10, 132, 255, 0.3)' : 'transparent'}; transition:all 0.15s ease;" class="ops-srv-row">
                <div style="display:flex; align-items:center; gap:6px; min-width:0; flex:1;">
                  <input type="checkbox" class="probe-checkbox ops-srv-checkbox" data-srv-id="${srv.id}" ${isChecked ? 'checked' : ''} onchange="toggleOpsServerSelection('${srv.id}', this.checked)">
                  <span class="status-dot-mini" style="background:${isConn ? 'var(--system-green)' : 'var(--system-orange)'};"></span>
                  <span style="font-size:11px; font-weight:500; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${srv.name}">${srv.name}</span>
                </div>
                <span style="font-size:10px; font-family:var(--font-mono); color:var(--text-secondary); margin-left:6px; flex-shrink:0;">${srv.host}</span>
              </label>
            `;
          }).join('')}
        </div>
      </div>
    `;
  });

  // 2. Standalone / Unbound Servers
  const nodeIds = new Set(allNodes.map(n => n.id));
  let unbounds = allSysServers.filter(s => !s.node_id || !nodeIds.has(s.node_id));
  if (keyword) {
    unbounds = unbounds.filter(s => 
      (s.name && s.name.toLowerCase().includes(keyword)) ||
      (s.host && s.host.toLowerCase().includes(keyword))
    );
  }

  if (unbounds.length > 0) {
    const unboundIds = unbounds.map(s => s.id);
    const allChecked = unboundIds.length > 0 && unboundIds.every(id => state.ops_selected_server_ids.has(id));

    html += `
      <div class="ops-node-group" style="border:1px solid var(--border-subtle); border-radius:8px; overflow:hidden; background:var(--surface-secondary); margin-top:2px;">
        <!-- Standalone Header -->
        <div style="display:flex; align-items:center; justify-content:space-between; padding:6px 10px; background:rgba(0,0,0,0.03); border-bottom:1px solid var(--border-subtle);">
          <label style="display:flex; align-items:center; gap:6px; cursor:pointer; min-width:0; flex:1;">
            <input type="checkbox" class="probe-checkbox" ${allChecked ? 'checked' : ''} onchange="toggleOpsStandaloneSelection(this.checked)">
            <span class="micro-capsule capsule-gray" style="font-size:8px; padding:0 4px; line-height:13px;">独立</span>
            <span style="font-size:11.5px; font-weight:600; color:var(--text-primary);">独立服务器 (${unbounds.length})</span>
          </label>
        </div>

        <!-- Standalone Servers List -->
        <div style="display:flex; flex-direction:column; padding:4px 6px; gap:3px;">
          ${unbounds.map(srv => {
            const isChecked = state.ops_selected_server_ids.has(srv.id);
            const isConn = srv.connected;
            return `
              <label style="display:flex; align-items:center; justify-content:space-between; padding:4px 8px; border-radius:5px; cursor:pointer; background:${isChecked ? 'rgba(10, 132, 255, 0.08)' : 'var(--surface-primary)'}; border:1px solid ${isChecked ? 'rgba(10, 132, 255, 0.3)' : 'transparent'}; transition:all 0.15s ease;" class="ops-srv-row">
                <div style="display:flex; align-items:center; gap:6px; min-width:0; flex:1;">
                  <input type="checkbox" class="probe-checkbox ops-srv-checkbox" data-srv-id="${srv.id}" ${isChecked ? 'checked' : ''} onchange="toggleOpsServerSelection('${srv.id}', this.checked)">
                  <span class="status-dot-mini" style="background:${isConn ? 'var(--system-green)' : 'var(--system-orange)'};"></span>
                  <span style="font-size:11px; font-weight:500; color:var(--text-primary); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${srv.name}">${srv.name}</span>
                </div>
                <span style="font-size:10px; font-family:var(--font-mono); color:var(--text-secondary); margin-left:6px; flex-shrink:0;">${srv.host}</span>
              </label>
            `;
          }).join('')}
        </div>
      </div>
    `;
  }

  container.innerHTML = html;
}

window.toggleOpsServerSelection = function(srvId, isChecked) {
  if (isChecked) {
    state.ops_selected_server_ids.add(srvId);
  } else {
    state.ops_selected_server_ids.delete(srvId);
  }
  updateOpsSelectionBadge();
  renderOpsServerTree();
};

window.toggleOpsNodeSelection = function(nodeId, isChecked) {
  const bound = (state.system_servers || []).filter(s => s.node_id === nodeId);
  bound.forEach(s => {
    if (isChecked) {
      state.ops_selected_server_ids.add(s.id);
    } else {
      state.ops_selected_server_ids.delete(s.id);
    }
  });
  updateOpsSelectionBadge();
  renderOpsServerTree();
};

window.toggleOpsStandaloneSelection = function(isChecked) {
  const nodeIds = new Set((state.servers || []).map(n => n.id));
  const unbounds = (state.system_servers || []).filter(s => !s.node_id || !nodeIds.has(s.node_id));
  unbounds.forEach(s => {
    if (isChecked) {
      state.ops_selected_server_ids.add(s.id);
    } else {
      state.ops_selected_server_ids.delete(s.id);
    }
  });
  updateOpsSelectionBadge();
  renderOpsServerTree();
};

function updateOpsSelectionBadge() {
  const countBadge = document.getElementById('opsTargetCountBadge');
  if (countBadge) {
    const count = state.ops_selected_server_ids.size;
    countBadge.textContent = `已选 ${count} 台服务器`;
    countBadge.className = count > 0 ? 'badge badge-cool' : 'badge badge-normal';
  }
}

let _opsEventsBound = false;
function initOpsEventHandlers() {
  if (_opsEventsBound) return;
  _opsEventsBound = true;

  // Search input
  const searchInput = document.getElementById('opsServerSearchInput');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      state.ops_filter_keyword = e.target.value;
      renderOpsServerTree();
    });
  }

  // Select all / None
  const btnSelectAll = document.getElementById('btnOpsSelectAll');
  if (btnSelectAll) {
    btnSelectAll.addEventListener('click', () => {
      (state.system_servers || []).forEach(s => state.ops_selected_server_ids.add(s.id));
      updateOpsSelectionBadge();
      renderOpsServerTree();
    });
  }

  const btnSelectNone = document.getElementById('btnOpsSelectNone');
  if (btnSelectNone) {
    btnSelectNone.addEventListener('click', () => {
      state.ops_selected_server_ids.clear();
      updateOpsSelectionBadge();
      renderOpsServerTree();
    });
  }

  // Preset commands
  document.querySelectorAll('.ops-preset-cmd').forEach(btn => {
    btn.addEventListener('click', () => {
      const cmd = btn.dataset.cmd;
      const input = document.getElementById('opsCommandInput');
      if (input && cmd) {
        input.value = cmd;
        input.focus();
      }
    });
  });

  // Execute button
  const btnExecute = document.getElementById('btnOpsExecuteNow');
  if (btnExecute) {
    btnExecute.addEventListener('click', runBatchOpsCommand);
  }

  // Clear output
  const btnClear = document.getElementById('btnOpsClearOutputs');
  if (btnClear) {
    btnClear.addEventListener('click', () => {
      const deck = document.getElementById('opsOutputDeck');
      if (deck) {
        deck.innerHTML = `
          <div style="color:#8b949e; text-align:center; padding:50px 0;">
            终端回显已清空。输入命令并点击「立即批量执行」开始下发。
          </div>
        `;
      }
      const summary = document.getElementById('opsExecutionSummaryBadge');
      if (summary) {
        summary.textContent = '就绪';
        summary.className = 'badge badge-normal';
      }
    });
  }

  // Copy output
  const btnCopy = document.getElementById('btnOpsCopyOutputs');
  if (btnCopy) {
    btnCopy.addEventListener('click', () => {
      const deck = document.getElementById('opsOutputDeck');
      if (deck) {
        navigator.clipboard.writeText(deck.innerText).then(() => {
          showToast('已复制全部终端输出内容到剪贴板', 'info');
        }).catch(() => {
          showToast('复制失败，请手动选取文本', 'warning');
        });
      }
    });
  }
}

async function runBatchOpsCommand() {
  const input = document.getElementById('opsCommandInput');
  const command = input ? input.value.trim() : '';

  if (!command) {
    showToast('请输入待执行的 Shell 命令', 'warning');
    if (input) input.focus();
    return;
  }

  const targetIds = Array.from(state.ops_selected_server_ids);
  if (targetIds.length === 0) {
    showToast('请在左侧勾选至少一台目标服务器', 'warning');
    return;
  }

  const deck = document.getElementById('opsOutputDeck');
  const summary = document.getElementById('opsExecutionSummaryBadge');
  const btnExecute = document.getElementById('btnOpsExecuteNow');

  if (summary) {
    summary.textContent = `执行中 (0/${targetIds.length})...`;
    summary.className = 'badge badge-warm';
  }
  if (btnExecute) {
    btnExecute.disabled = true;
    btnExecute.innerHTML = `
      <span class="spinner-inline" style="display:inline-block; width:12px; height:12px; border:2px solid rgba(255,255,255,0.3); border-top-color:#fff; border-radius:50%; animation:spin 0.8s linear infinite; margin-right:4px;"></span>
      <span>正在批量下发...</span>
    `;
  }

  if (deck) {
    deck.innerHTML = `
      <div style="color:#58a6ff; font-weight:600; padding-bottom:6px; border-bottom:1px dashed #30363d;">
        🚀 正在向 ${targetIds.length} 台服务器分发命令: <span style="color:#f0883e;">${command}</span>
      </div>
    `;
  }

  try {
    const res = await callApi('execute_batch_ssh', targetIds, command);
    if (!res || !res.success) {
      if (deck) {
        deck.innerHTML += `
          <div style="color:#f85149; margin-top:8px;">❌ 下发失败: ${res?.message || '未知通信故障'}</div>
        `;
      }
      if (summary) {
        summary.textContent = '执行失败';
        summary.className = 'badge badge-danger';
      }
      showToast(res?.message || '执行失败', 'danger');
      return;
    }

    const results = res.results || [];
    let successCount = 0;
    let failCount = 0;

    let cardsHtml = `
      <div style="color:#58a6ff; font-weight:600; padding-bottom:6px; border-bottom:1px dashed #30363d;">
        🚀 命令分发完成: <span style="color:#f0883e;">${command}</span>
      </div>
    `;

    results.forEach(r => {
      if (r.success) successCount++;
      else failCount++;

      const isOk = r.success;
      const statusColor = isOk ? '#3fb950' : '#f85149';
      const statusText = isOk ? `成功 (exit ${r.exit_code})` : `失败 (${r.error || 'exit ' + r.exit_code})`;

      cardsHtml += `
        <div style="background:#161b22; border-radius:6px; border:1px solid ${isOk ? '#30363d' : '#8b1a10'}; overflow:hidden; margin-top:6px;">
          <!-- Server Terminal Header -->
          <div style="display:flex; justify-content:space-between; align-items:center; background:#21262d; padding:4px 10px; font-size:11px;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="font-weight:600; color:#e6edf3;">${r.server_name}</span>
              <span style="color:#8b949e;">(${r.host})</span>
            </div>
            <span style="color:${statusColor}; font-weight:600;">${statusText}</span>
          </div>
          <!-- Terminal Body -->
          <div style="padding:8px 10px; max-height:220px; overflow-y:auto; white-space:pre-wrap; word-break:break-all; line-height:1.45;">
            ${r.stdout ? `<div style="color:#c9d1d9;">${escapeHtml(r.stdout.trim())}</div>` : ''}
            ${r.stderr ? `<div style="color:#f85149; margin-top:4px;">${escapeHtml(r.stderr.trim())}</div>` : ''}
            ${!r.stdout && !r.stderr && r.error ? `<div style="color:#f85149;">${escapeHtml(r.error)}</div>` : ''}
            ${!r.stdout && !r.stderr && !r.error ? `<div style="color:#8b949e;">(命令无输出)</div>` : ''}
          </div>
        </div>
      `;
    });

    if (deck) deck.innerHTML = cardsHtml;

    if (summary) {
      summary.textContent = `完成: 成功 ${successCount} / 失败 ${failCount}`;
      summary.className = failCount === 0 ? 'badge badge-cool' : 'badge badge-warm';
    }

    showToast(`批量执行完毕: 成功 ${successCount} 台, 失败 ${failCount} 台`, failCount === 0 ? 'info' : 'warning');
  } catch (err) {
    if (deck) {
      deck.innerHTML += `
        <div style="color:#f85149; margin-top:8px;">❌ 网络通信异常: ${String(err)}</div>
      `;
    }
    if (summary) {
      summary.textContent = '异常中断';
      summary.className = 'badge badge-danger';
    }
    showToast(`下发异常: ${String(err)}`, 'danger');
  } finally {
    if (btnExecute) {
      btnExecute.disabled = false;
      btnExecute.innerHTML = `
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" style="margin-right:4px;">
          <polygon points="5 3 19 12 5 21 5 3"/>
        </svg>
        <span>立即批量执行</span>
      `;
    }
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
}

// Copy QQ Group Number to clipboard
window.copyQQGroupNumber = function() {
  const qq = '317151818';
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(qq).then(() => {
      showToast('QQ 交流群号 317151818 已复制到剪贴板！', 'success');
    }).catch(() => {
      fallbackCopyText(qq);
    });
  } else {
    fallbackCopyText(qq);
  }
};

function fallbackCopyText(text) {
  const input = document.createElement('input');
  input.value = text;
  document.body.appendChild(input);
  input.select();
  try {
    document.execCommand('copy');
    showToast(`QQ 交流群号 ${text} 已复制到剪贴板！`, 'success');
  } catch (e) {
    showToast(`请手动复制 QQ 群号: ${text}`, 'info');
  }
  document.body.removeChild(input);
}
