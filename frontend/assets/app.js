const state = {
  token: localStorage.getItem('dds_token') || '',
  me: null,
  view: 'dashboard',
  overview: {},
  courses: [],
  imports: [],
  reports: [],
  warnings: [],
  interventions: [],
  questionnaires: [],
  questionnaire: null,
  questionnaireAnswers: {},
  silentRequests: [],
  referrals: [],
  audit: [],
  settings: {},
  currentReport: null,
  growth: null,
  growthStudentNo: '',
  interventionDraft: null,
  users: [],
  importResult: null,
  message: null,
};

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function fmtDateTime(value) {
  if (!value) return '—';
  return String(value).replace('T', ' ').slice(0, 19);
}

function toast(message, isError = false) {
  state.message = { text: message, error: isError };
  window.setTimeout(() => {
    state.message = null;
    const node = document.querySelector('.toast');
    if (node) node.remove();
  }, 4200);
  render();
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.json);
    delete options.json;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return {};
  const isJson = (response.headers.get('content-type') || '').includes('json');
  const data = isJson ? await response.json() : await response.text();
  if (!response.ok) throw new Error((data && data.error) || `请求失败 ${response.status}`);
  return data;
}

const teacherViews = [
  ['dashboard', '工作台'],
  ['courses', '课程管理'],
  ['imports', '数据采集'],
  ['reports', '报表闭环'],
  ['warnings', '预警中心'],
  ['growth', '成长画像'],
  ['mind', '量表与育心'],
  ['manual', '使用手册'],
  ['settings', '参数配置'],
  ['users', '用户与角色'],
  ['audit', '审计留痕'],
];

const adminViews = teacherViews;
const studentViews = [
  ['dashboard', '我的工作台'],
  ['reports', '我的成长报告'],
  ['growth', '成长曲线'],
  ['mind', '量表与静默通道'],
  ['manual', '使用手册'],
];

function viewsForRole(role) {
  if (role === 'student') return studentViews;
  if (role === 'admin') return adminViews;
  return teacherViews.filter(([name]) => !['audit', 'users'].includes(name));
}

async function login(event) {
  event.preventDefault();
  const form = event.target;
  const payload = {
    username: form.username.value.trim(),
    password: form.password.value,
  };
  try {
    const data = await api('/api/auth/login', { method: 'POST', json: payload });
    state.token = data.token;
    state.me = data.user;
    localStorage.setItem('dds_token', state.token);
    state.view = 'dashboard';
    await refreshView(true);
    toast('登录成功');
  } catch (error) {
    toast(error.message, true);
  }
}

async function logout() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
  state.token = '';
  state.me = null;
  state.currentReport = null;
  localStorage.removeItem('dds_token');
  render();
}

async function refreshView(force = false) {
  if (!state.me) return;
  try {
    if (['dashboard', 'reports', 'imports', 'warnings', 'settings'].includes(state.view) || force) {
      state.overview = await api('/api/overview');
    }
    if (state.me.role !== 'student') {
      if (['dashboard', 'courses', 'imports', 'reports', 'warnings', 'growth', 'mind', 'settings'].includes(state.view) || force) {
        const courses = await api('/api/courses');
        state.courses = courses.items || [];
      }
      if (['imports'].includes(state.view) || force) {
        const imports = await api('/api/imports');
        state.imports = imports.items || [];
      }
      if (['reports', 'dashboard'].includes(state.view) || force) {
        const reports = await api('/api/reports');
        state.reports = reports.items || [];
      }
      if (['warnings'].includes(state.view) || force) {
        const warnings = await api('/api/warnings');
        state.warnings = warnings.items || [];
      }
      if (state.view === 'users' || (force && state.me.role === 'admin')) {
        const users = await api('/api/users');
        state.users = users.items || [];
      }
    } else {
      const reports = await api('/api/reports');
      state.reports = reports.items || [];
    }
    if (state.view === 'mind' || force) {
      const questionnaires = await api('/api/questionnaires');
      state.questionnaires = questionnaires.items || [];
      const silent = await api('/api/silent-requests');
      state.silentRequests = silent.items || [];
      if (state.me.role !== 'student') {
        const referrals = await api('/api/referrals');
        state.referrals = referrals.items || [];
        state.users = referrals.assignable_users || [];
      }
    }
    if (state.view === 'settings' || (force && state.me.role !== 'student')) {
      state.settings = await api('/api/settings');
    }
    if (state.view === 'audit') {
      const audit = await api('/api/audit');
      state.audit = audit.items || [];
    }
  } catch (error) {
    if (error.message.includes('登录')) await logout();
    else toast(error.message, true);
  }
}

async function setView(view) {
  state.view = view;
  await refreshView();
  render();
  window.scrollTo(0, 0);
}

function renderLogin() {
  return `
    <section class="login-page">
      <div class="login-card">
        <div class="brand-mark">双动赋能 · 双驱育心 · 轻量级 V2.1</div>
        <h1>双动态教学评估平台</h1>
        <p class="sub">采集 → 计算 → 核定 → 发布 → 预警 → 育心留痕</p>
        <form id="login-form">
          <label>用户名<input name="username" required autocomplete="username" placeholder="teacher" /></label>
          <label>密码<input name="password" type="password" required autocomplete="current-password" placeholder="teacher123456" /></label>
          <button class="button" type="submit">登录</button>
        </form>
        <p class="small muted" style="margin-bottom:0">首次启动演示账号：admin/admin123456、teacher/teacher123456、student/student123456。生产使用请立即修改密码。</p>
      </div>
    </section>
  `;
}

function renderShell(content) {
  const views = viewsForRole(state.me.role);
  const nav = views.map(([name, label]) => `
    <button class="nav-button ${state.view === name ? 'active' : ''}" data-action="nav" data-view="${name}">${label}</button>
  `).join('');
  return `
    <div class="app-shell">
      <aside class="sidebar">
        <h1>双动赋能<br/>双驱育心平台</h1>
        ${nav}
        <div class="sidebar-footer">
          低利害铁律已启用<br/>全站无排名接口
        </div>
      </aside>
      <main class="main">
        <div class="topbar">
          <div>
            <h2>${esc(viewTitle())}</h2>
            <div class="topbar-sub">${esc(viewSubtitle())}</div>
          </div>
          <div class="user-box">
            <strong>${esc(state.me.display_name)}</strong>
            ${esc(roleName(state.me.role))} · ${esc(state.me.username)}
            <div><button class="button secondary small" data-action="logout">退出</button></div>
          </div>
        </div>
        ${content}
      </main>
    </div>
  `;
}

function viewTitle() {
  const titles = {
    dashboard: '工作台', courses: '课程管理', imports: '数据采集与适配', reports: state.currentReport ? '报表详情' : '报表闭环',
    warnings: '预警中心', growth: '成长画像', mind: '量表与育心工作流', manual: '使用手册', settings: '算法与制度参数', users: '用户与角色', audit: '审计留痕',
    questionnaire: '量表填写',
  };
  return titles[state.view] || '平台';
}

function viewSubtitle() {
  return {
    dashboard: '闭环运行状态与低利害制度提示',
    courses: '按课程归集班级、数据源与报表',
    imports: '雨课堂原始导出 / 标准 CSV / 历史 IDG 表',
    reports: '教师核定后才能发布；发布 24 小时内可撤回',
    warnings: '成绩三色与系数预警相互独立',
    growth: '只与自身基线比较，不提供排名或位次',
    mind: '量表结果进入静默通道，绝不进入成绩报表',
    manual: '新教师、新学生和管理员快速上手',
    settings: '阈值、权重、窗口与防刷课参数',
    users: '管理员统一创建账号、分派角色、停用或重置密码',
    audit: '导入、核定、发布、导出、转介全程留痕',
  }[state.view] || '';
}

function roleName(role) {
  return { admin: '管理员', teacher: '教师', student: '学生' }[role] || role;
}

function statusChip(status) {
  const map = {
    draft: ['gray', '待核定'], reviewed: ['blue', '已核定'], published: ['green', '已发布'], revoked: ['red', '已撤回'],
  };
  const [color, label] = map[status] || ['gray', status];
  return `<span class="chip ${color}">${esc(label)}</span>`;
}

function importStatusChip(status) {
  const map = {
    success: ['green', '成功'],
    running: ['blue', '处理中'],
    failed: ['red', '失败'],
  };
  const [color, label] = map[status] || ['gray', status];
  return colorChip(color, label);
}

function colorChip(color, label) {
  return `<span class="chip ${esc(color)}">${esc(label)}</span>`;
}

function scoreChip(value) {
  const numeric = Number(value || 0);
  const color = numeric >= 90 ? 'green' : numeric >= 60 ? 'blue' : 'red';
  return colorChip(color, Number(value).toFixed(0));
}

function coeffChip(value) {
  if (value === null || value === undefined) return '<span class="chip gray">缺失</span>';
  const numeric = Number(value);
  const color = numeric < 0.5 ? 'red' : numeric < 0.8 ? 'blue' : 'green';
  return colorChip(color, numeric.toFixed(2));
}

function renderDashboard() {
  const data = state.overview || {};
  const stats = state.me.role === 'student' ? [
    ['可查看报告', data.reports || 0],
    ['自身基线', '强制显示'],
    ['排名视图', '制度排除'],
    ['静默通道', '开放'],
  ] : [
    ['课程', data.courses || 0],
    ['报表', data.reports || 0],
    ['预警学生记录', data.warnings || 0],
    ['未闭环转介', data.open_referrals || 0],
  ];
  const binding = state.me.role === 'student' && !state.me.student_id ? `
    <section class="card" style="margin-bottom:18px">
      <h3>绑定学号</h3>
      <form id="binding-form" class="form-grid">
        <label>学号<input name="student_no" required placeholder="请输入教师已导入的学号" /></label>
        <label>姓名<input name="name" required placeholder="用于校验身份" /></label>
        <div><button class="button" type="submit">绑定</button></div>
      </form>
    </section>` : '';
  const password = `
    <section class="card" style="margin-bottom:18px">
      <h3>修改登录密码</h3>
      <form id="password-form" class="form-grid">
        <label>原密码<input name="old_password" type="password" required /></label>
        <label>新密码（至少10位）<input name="new_password" type="password" minlength="10" required /></label>
        <div style="align-self:end"><button class="button secondary" type="submit">修改密码</button></div>
      </form>
    </section>`;
  return `
    <div class="notice">本平台评价结果默认低利害：不生成班级排名，不用于评优、人事、薪酬或问责；学生端只看自身成长轨迹。</div>
    ${binding}
    ${password}
    <div class="grid cols-4">
      ${stats.map(([label, value]) => `<div class="card"><div class="stat-value">${esc(value)}</div><div class="stat-label">${esc(label)}</div></div>`).join('')}
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <section class="card">
        <h3>MVP 一期闭环</h3>
        <div class="pill-list">
          ${(data.closed_loop || ['采集','计算','核定','发布','预警','育心留痕']).map((x, i) => `<span class="chip ${i % 2 ? 'blue' : 'green'}">${esc(x)}</span>`).join('')}
        </div>
        <p class="small muted">单班采集后可即时重算 IDG/ADG；发布动作被后端状态机物理约束。</p>
      </section>
      <section class="card">
        <h3>当前边界</h3>
        <div class="kv">
          <strong>数据源</strong><span>权威源仍是雨课堂/学习通等；本系统只采集加工</span>
          <strong>心理危机</strong><span>处置主体为学校心理中心；本系统预警/转介/留痕</span>
          <strong>学生心理结果</strong><span>静默通道，不直接发布</span>
        </div>
      </section>
    </div>
  `;
}

function renderCourses() {
  return `
    <section class="card" style="margin-bottom:18px">
      <h3>新建课程</h3>
      <form id="course-form" class="form-grid">
        <label>课程名称<input name="name" required placeholder="无机化学" /></label>
        <label>课程代码<input name="code" required placeholder="WHXX-2025" /></label>
        <label>数据源<select name="source_platform"><option>雨课堂</option><option>超星学习通</option><option>智慧树</option><option>国家智慧教育平台</option><option>Excel 模板</option></select></label>
        <label>学年<input name="academic_year" placeholder="2025-2026" /></label>
        <div style="align-self:end"><button class="button" type="submit">创建课程</button></div>
      </form>
    </section>
    <section class="card">
      <h3>课程列表</h3>
      ${courseTable()}
    </section>
  `;
}

function courseOptions(selectedId = '') {
  return state.courses.map(course => `
    <option value="${course.id}" ${String(course.id) === String(selectedId) ? 'selected' : ''}>${esc(course.name)} / ${esc(course.code)}</option>
  `).join('');
}

function courseTable() {
  if (!state.courses.length) return '<div class="empty">暂无课程</div>';
  return `<div class="table-wrap"><table><thead><tr><th>课程</th><th>代码</th><th>数据源</th><th>学生</th><th>报表</th><th>创建时间</th></tr></thead><tbody>
    ${state.courses.map(c => `<tr><td>${esc(c.name)}</td><td>${esc(c.code)}</td><td>${esc(c.source_platform)}</td><td>${c.student_count}</td><td>${c.report_count}</td><td>${esc(fmtDateTime(c.created_at))}</td></tr>`).join('')}
  </tbody></table></div>`;
}

function renderImports() {
  const result = state.importResult ? `<div class="notice" style="margin-top:14px">导入完成：${esc(JSON.stringify(state.importResult))}</div>` : '';
  return `
    <div class="notice">若平台接口暂不可用，可使用 Excel/CSV 模板降级导入，不中断计算闭环。雨课堂“数据汇总”Sheet 可直接上传。</div>
    <section class="card">
      <h3>上传过程数据</h3>
      <form id="import-form">
        <div class="form-grid">
          <label>课程<select name="course_id" required>${courseOptions()}</select></label>
          <label>导入类型<select name="kind"><option value="realtime">即时过程数据</option><option value="history">历史 IDG 表</option></select></label>
          <label>即时日期<input name="period_date" type="date" value="${new Date().toISOString().slice(0,10)}" /></label>
          <label>历史年份<input name="history_year" type="number" min="2000" max="2100" value="2025" /></label>
        </div>
        <div class="form-grid">
          <label>班级/学号筛选<input name="student_filter" placeholder="如 25药生；留空导入全部" /></label>
          <label>文件<input name="file" type="file" accept=".xlsx,.xlsm,.csv" required /></label>
          <div class="file-help" style="align-self:end"><a href="/api/template.csv" download>下载标准 CSV 模板</a></div>
          <div style="align-self:end"><button class="button" type="submit">开始导入</button></div>
        </div>
      </form>
      ${result}
    </section>
    <section class="card" style="margin-top:18px">
      <h3>导入记录</h3>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>课程</th><th>类型</th><th>文件</th><th>日期</th><th>行数</th><th>状态</th><th>提示</th><th>操作</th></tr></thead><tbody>
        ${state.imports.length ? state.imports.map(item => `<tr>
          <td>${esc(fmtDateTime(item.created_at))}</td><td>${esc(item.course_name)}</td><td>${item.kind === 'realtime' ? '即时' : '历史'}</td>
          <td>${esc(item.filename)}</td><td>${esc(item.period_date || '—')}</td><td>${item.rows_imported}/${item.rows_read}</td>
          <td>${importStatusChip(item.status)}</td>
          <td>${item.warnings.map(w => esc(w.message)).join('；') || '—'}</td>
          <td><button class="button danger small" data-action="delete-import" data-id="${item.id}">删除</button></td>
        </tr>`).join('') : '<tr><td colspan="9" class="empty">暂无导入</td></tr>'}
      </tbody></table></div>
    </section>
  `;
}

function renderReports() {
  if (state.currentReport) return renderReportDetail();
  const teacherForm = state.me.role === 'student' ? '' : `
    <section class="card" style="margin-bottom:18px">
      <h3>生成双动态报表</h3>
      <form id="report-form" class="form-grid">
        <label>课程<select name="course_id" required>${courseOptions()}</select></label>
        <label>评价日期<input name="period_date" type="date" required value="${new Date().toISOString().slice(0,10)}" /></label>
        <div style="align-self:end"><button class="button" type="submit">计算 IDG / ADG</button></div>
      </form>
    </section>`;
  return `
    ${teacherForm}
    <section class="card">
      <h3>${state.me.role === 'student' ? '我的已发布报告' : '报表列表'}</h3>
      <div class="table-wrap"><table><thead><tr><th>课程</th><th>日期</th><th>状态</th><th>人数</th><th>颜色分布</th><th>预警</th><th>操作</th></tr></thead><tbody>
        ${state.reports.length ? state.reports.map(report => `<tr>
          <td>${esc(report.course_name)}</td><td>${esc(report.period_date)}</td><td>${statusChip(report.status)}</td>
          <td>${report.summary?.total || 0}</td>
          <td>${colorChip('green', report.summary?.colors?.green || 0)} ${colorChip('blue', report.summary?.colors?.blue || 0)} ${colorChip('red', report.summary?.colors?.red || 0)}</td>
          <td>${report.summary?.warning_count || 0}</td>
          <td><button class="button secondary small" data-action="open-report" data-id="${report.id}">查看</button></td>
        </tr>`).join('') : '<tr><td colspan="7" class="empty">暂无报表</td></tr>'}
      </tbody></table></div>
    </section>
  `;
}

function evidenceText(evidence) {
  if (!evidence || typeof evidence !== 'object') return '—';
  return Object.entries(evidence).map(([key, value]) => {
    if (value && typeof value === 'object' && !Array.isArray(value) && 'numerator' in value) {
      return `${key}: ${value.numerator ?? '缺失'}/${value.denominator ?? '缺失'}`;
    }
    return '';
  }).filter(Boolean).join('；') || '—';
}

function renderReportDetail() {
  const report = state.currentReport;
  const canReview = ['admin','teacher'].includes(state.me.role);
  const actions = canReview ? `
    <div class="button-row">
      ${report.status === 'draft' ? '<button class="button success" data-action="review-report">教师核定</button>' : ''}
      ${report.status === 'reviewed' ? '<button class="button" data-action="publish-report">发布学生端</button>' : ''}
      ${report.status === 'published' ? '<button class="button danger" data-action="revoke-report">24小时内撤回</button>' : ''}
      <button class="button secondary" data-action="export-report">低利害导出</button>
      <button class="button secondary" data-action="close-report">返回列表</button>
    </div>` : '<button class="button secondary" data-action="close-report">返回列表</button>';
  return `
    <div class="notice">默认无排名视图：行序为班级与学号，仅用于查漏补缺与个体支持。导出会再次确认并写入审计。</div>
    ${report.status === 'published' ? '<div class="notice">学生查看路径：学生账号登录 → 绑定学号和姓名 → “我的成长报告”。学生只会看到本人这一行。</div>' : ''}
    <div class="report-grid">
      <section class="card">
        <div class="kv" style="margin-bottom:14px">
          <strong>课程</strong><span>${esc(report.course_name)}</span>
          <strong>日期</strong><span>${esc(report.period_date)}</span>
          <strong>状态</strong><span>${statusChip(report.status)}</span>
          <strong>配置快照</strong><span class="small">绿≥${report.summary?.config_snapshot?.green_threshold}，蓝≥${report.summary?.config_snapshot?.blue_threshold}；ADG 权重（近→远）${esc((report.summary?.config_snapshot?.adg_weights_recent_to_old || []).join('/'))}</span>
        </div>
        ${actions}
        <div style="margin-top:16px" class="table-wrap"><table>
          <thead><tr><th>班级</th><th>学号</th><th>姓名</th><th>课程%</th><th>课件系数</th><th>公告系数</th><th>视频</th><th>互动</th><th>IDG</th><th>ADG</th><th>状态</th><th>分子/分母</th><th>预警</th><th>操作</th></tr></thead>
          <tbody>${report.items.map(item => `<tr>
            <td>${esc(item.class_name)}</td><td>${esc(item.student_no)}</td><td>${esc(item.name)}</td>
            <td>${item.course_percent.toFixed(2)}</td><td>${coeffChip(item.material_coeff)}</td><td>${coeffChip(item.announcement_coeff)}</td>
            <td>${coeffChip(item.video_coeff)}</td><td>${coeffChip(item.interaction_coeff)}</td>
            <td>${scoreChip(item.idg)}</td><td>${scoreChip(item.adg)}</td><td>${esc(item.state_type)}</td>
            <td class="small">${esc(evidenceText(item.evidence))}</td>
            <td>${(item.flags || []).filter(x => x.scope === 'coefficient' || x.scope === 'pre_warning' || x.scope === 'observation').map(x => `<span class="chip ${x.severity === 'high' ? 'red' : 'amber'}">${esc(x.message)}</span>`).join(' ') || '—'}</td>
            <td>${canReview ? `<button class="button secondary small" data-action="growth" data-id="${item.student_no}">曲线</button><button class="button secondary small" data-action="intervention" data-id="${item.id}" data-student="${item.student_id}" data-student-no="${item.student_no}" data-course="${report.course_id}">干预</button>` : `<button class="button secondary small" data-action="growth" data-id="${item.student_no}">曲线</button>`}</td>
          </tr>`).join('')}</tbody>
        </table></div>
      </section>
      <aside class="grid" style="align-content:start">
        <section class="card"><h3>汇总</h3>
          <div class="kv"><strong>人数</strong><span>${report.items.length}</span><strong>绿</strong><span>${report.summary?.colors?.green || 0}</span><strong>蓝</strong><span>${report.summary?.colors?.blue || 0}</span><strong>红</strong><span>${report.summary?.colors?.red || 0}</span></div>
        </section>
        <section class="card"><h3>状态分流</h3>
          ${Object.entries(report.summary?.states || {}).map(([name, count]) => `<div class="dimension"><span>${esc(name)}</span><strong>${count}</strong></div>`).join('') || '<div class="empty">暂无</div>'}
        </section>
      </aside>
    </div>
  `;
}

function renderWarnings() {
  return `
    <div class="notice warn">两套预警不混用：成绩三色看 IDG/ADG；系数蓝/红区只作用于 [0,1] 系数。同一学生的多项系数/观测预警已合并为一行。</div>
    <section class="card"><h3>预警明细</h3>
      <div class="table-wrap"><table><thead><tr><th>日期</th><th>课程</th><th>班级</th><th>学生</th><th>IDG / ADG</th><th>预警项</th><th>操作</th></tr></thead><tbody>
      ${state.warnings.length ? state.warnings.map(w => `<tr>
        <td>${esc(w.period_date)}</td><td>${esc(w.course_name)}</td><td>${esc(w.class_name)}</td><td>${esc(w.name)} ${esc(w.student_no)}</td>
        <td>${scoreChip(w.idg)} / ${scoreChip(w.adg)}</td>
        <td>${w.warnings.map(x => `<span class="chip ${x.severity === 'high' ? 'red' : 'amber'}">${esc(x.message)}</span>`).join(' ')}</td>
        <td><button class="button secondary small" data-action="growth" data-id="${w.student_no}">曲线</button>
            <button class="button small" data-action="intervention" data-id="${w.report_item_id}" data-student="${w.student_id}" data-student-no="${w.student_no}" data-course="${w.course_id}">登记干预</button></td>
      </tr>`).join('') : '<tr><td colspan="7" class="empty">暂无预警</td></tr>'}
      </tbody></table></div>
    </section>
  `;
}

function renderGrowth() {
  const isStudent = state.me.role === 'student';
  const growth = state.growth;
  return `
    <section class="card" style="margin-bottom:18px">
      <h3>选择学生</h3>
      ${isStudent ? '<p class="small muted">学生端自动显示本人曲线。</p>' : `
        <form id="growth-form" class="form-grid">
          <label>学生学号<input name="student_no" required value="${esc(state.growthStudentNo)}" placeholder="例如 S20240001" /></label>
          <div style="align-self:end"><button class="button" type="submit">查看成长曲线</button></div>
        </form>`}
    </section>
    ${growth ? `
      <div class="report-grid">
        <section class="card"><h3>自身成长趋势</h3>${growthSvg(growth.trend || [])}
          <p class="small muted">学号 ${esc(growth.student?.student_no || '')}；基线 ${esc(growth.baseline ?? '—')}；仅比较自身，不呈现班级位次。</p>
        </section>
        <section class="card"><h3>成长画像</h3>
          ${(growth.profile?.dimensions || []).map(dim => `
            <div class="dimension"><span>${esc(dim.name)}<div class="small muted">${esc(dim.evidence)}</div></span><strong>${dim.value === null ? '缺失' : Number(dim.value).toFixed(2)}</strong></div>
            <div class="bar"><span style="width:${dim.value === null ? 0 : Math.max(0, Math.min(1, dim.value))*100}%"></span></div>
          `).join('') || '<div class="empty">暂无画像</div>'}
          <p class="small muted">${esc(growth.profile?.note || '')}</p>
        </section>
      </div>` : '<section class="card"><div class="empty">请输入学生学号后查看曲线。</div></section>'}
  `;
}

function growthSvg(points) {
  if (!points.length) return '<div class="empty">暂无趋势点</div>';
  const width = 720, height = 280, padding = 42;
  const values = points.map(p => Number(p.idg || 0));
  const min = Math.min(0, ...values), max = Math.max(100, ...values);
  const x = i => padding + (points.length === 1 ? 0.5 : i / (points.length - 1)) * (width - padding * 2);
  const y = v => height - padding - ((v - min) / (max - min || 1)) * (height - padding * 2);
  const polyline = points.map((p, i) => `${x(i).toFixed(1)},${y(Number(p.idg || 0)).toFixed(1)}`).join(' ');
  const baselineY = y(Number(points[0].baseline || 0));
  const labels = points.filter((_, i) => points.length <= 10 || i % Math.ceil(points.length / 10) === 0).map(p => p.date);
  return `<svg class="trend-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="个体动态成绩趋势">
    <line x1="${padding}" y1="${y(60)}" x2="${width-padding}" y2="${y(60)}" stroke="#93c5fd" stroke-dasharray="6 5" />
    <line x1="${padding}" y1="${y(90)}" x2="${width-padding}" y2="${y(90)}" stroke="#86efac" stroke-dasharray="6 5" />
    <line x1="${padding}" y1="${baselineY}" x2="${width-padding}" y2="${baselineY}" stroke="#f59e0b" stroke-dasharray="4 5" />
    <polyline points="${polyline}" fill="none" stroke="#2563eb" stroke-width="3" />
    ${points.map((p, i) => `<circle cx="${x(i)}" cy="${y(Number(p.idg || 0))}" r="4" fill="#2563eb"><title>${esc(p.date)}：${p.idg}</title></circle>`).join('')}
    <text x="${padding}" y="${y(90)-6}" font-size="11" fill="#15803d">90</text>
    <text x="${padding}" y="${y(60)-6}" font-size="11" fill="#2563eb">60</text>
    ${labels.map(date => `<text x="${x(points.findIndex(p => p.date === date))}" y="${height-15}" font-size="10" fill="#64748b" text-anchor="middle">${esc(String(date).slice(5))}</text>`).join('')}
  </svg>`;
}

function renderMind() {
  const isStudent = state.me.role === 'student';
  return `
    <div class="notice warn">量表数据属于敏感信息：发布字段白名单硬校验，不进入成绩、不向学生端展示心理评价结果；危机规则触发后 2 小时内上报。</div>
    <div class="grid cols-2">
      <section class="card"><h3>量表清单</h3>
        <div class="table-wrap"><table><thead><tr><th>量表</th><th>状态</th><th>题量</th><th>操作</th></tr></thead><tbody>
        ${state.questionnaires.map(q => `<tr><td>${esc(q.title)}</td><td>${statusChip(q.status === 'published' ? 'published' : 'draft')}</td><td>${q.question_count}</td>
          <td><button class="button secondary small" data-action="open-questionnaire" data-id="${q.id}">打开</button></td></tr>`).join('') || '<tr><td colspan="4" class="empty">暂无</td></tr>'}
        </tbody></table></div>
        ${!isStudent ? `<details style="margin-top:14px"><summary class="small">创建/导入量表 JSON</summary>
          <form id="questionnaire-form" style="margin-top:10px">
            <label>标题<input name="title" required /></label>
            <label>说明<input name="description" /></label>
            <label>状态<select name="status"><option value="draft">草稿</option><option value="published">发布</option></select></label>
            <textarea name="questions" placeholder='[{"stem":"题目","options":["从不","偶尔","有时","经常","总是"],"reverse_scoring":false,"crisis_operator":"","crisis_value":null}]'></textarea>
            <button class="button" type="submit">保存量表</button>
          </form></details>` : ''}
      </section>
      <section class="card"><h3>静默通道申请</h3>
        ${isStudent ? `<form id="silent-form">
          <label>原因（可简写，可不暴露细节）<input name="reason" placeholder="近期希望减少公告提醒" /></label>
          <button class="button" type="submit">申请静默</button>
        </form>` : '<p class="small muted">教师端可审批学生静默申请；批准后发布提醒时将该学生移出公告推送名单。</p>'}
        <div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>时间</th><th>学生</th><th>原因</th><th>状态</th><th>操作</th></tr></thead><tbody>
        ${state.silentRequests.map(r => `<tr><td>${esc(fmtDateTime(r.created_at))}</td><td>${esc(r.name || r.display_name || '未绑定')}</td><td>${esc(r.reason || '—')}</td><td>${esc(r.status)}</td>
          ${!isStudent && r.status === 'pending' ? `<td><button class="button success small" data-action="silent-decide" data-id="${r.id}" data-status="approved">批准</button><button class="button danger small" data-action="silent-decide" data-id="${r.id}" data-status="rejected">拒绝</button></td>` : '<td>—</td>'}</tr>`).join('') || '<tr><td colspan="5" class="empty">暂无申请</td></tr>'}
        </tbody></table></div>
      </section>
    </div>
    ${!isStudent ? `<section class="card" style="margin-top:18px"><h3>心理转介工单</h3>
      <p class="small muted">工单默认分派给当前登录教师；也可从下拉选择其他教师/管理员。被分派老师登录后可看到并处理自己的工单，管理员可看全部，未分派工单所有教师可见以便认领。</p>
      <form id="referral-form" class="form-grid">
        <label>学生学号<input name="student_no" required placeholder="例如 S20240001" /></label>
        <label>原因<input name="reason" required /></label>
        <label>分派给<select name="assigned_to">${userOptions(state.me.id)}</select></label>
        <label>外部处理人<input name="handler" placeholder="学校心理中心/辅导员，可留空" /></label>
        <div style="align-self:end"><button class="button danger" type="submit">创建 2 小时工单</button></div>
      </form>
      <div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>创建</th><th>截止</th><th>学生</th><th>原因</th><th>状态</th><th>分派</th><th>处理</th></tr></thead><tbody>
      ${state.referrals.length ? state.referrals.map(r => `<tr>
        <td>${esc(fmtDateTime(r.created_at))}</td><td>${esc(fmtDateTime(r.due_at))}</td><td>${esc(r.name || '')} ${esc(r.student_no || '')}</td><td>${esc(r.reason || '—')}</td>
        <td><select data-referral-field="status">${['open','reported','completed','escalated'].map(x => `<option value="${x}" ${r.status===x?'selected':''}>${x}</option>`).join('')}</select></td>
        <td>${esc(r.assigned_name || '未分派/待认领')}</td>
        <td><input data-referral-field="notes" value="${esc(r.notes || '')}" placeholder="处理留痕" /><button class="button secondary small" data-action="update-referral" data-id="${r.id}">保存</button></td>
      </tr>`).join('') : '<tr><td colspan="7" class="empty">暂无</td></tr>'}
      </tbody></table></div></section>` : ''}
  `;
}

function userOptions(selectedId = '') {
  return (state.users || []).map(user => `
    <option value="${user.id}" ${String(user.id) === String(selectedId) ? 'selected' : ''}>${esc(user.display_name)}（${esc(user.username)} / ${esc(roleName(user.role))}）</option>
  `).join('');
}

function renderQuestionnaire() {
  const q = state.questionnaire;
  return `
    <button class="button secondary small" data-action="close-questionnaire" style="margin-bottom:14px">返回</button>
    <section class="card"><h3>${esc(q.title)}</h3><p class="muted">${esc(q.description)}</p>
      <form id="questionnaire-response-form" class="grid">
      ${q.questions.map(question => `<div class="question-block">
        <strong>${question.order_no}. ${esc(question.stem)}</strong>
        <div class="option-row" data-question="${question.id}">
          ${question.options.map((option, index) => `<button type="button" class="option ${state.questionnaireAnswers[question.id] === index ? 'active' : ''}" data-action="choose-option" data-question="${question.id}" data-index="${index}">${esc(option)}</button>`).join('')}
        </div></div>`).join('')}
      <button class="button" type="submit">匿名提交到静默通道</button>
      </form>
    </section>`;
}

function renderSettings() {
  const s = state.settings;
  return `
    <section class="card"><h3>可配置参数</h3>
      <form id="settings-form" class="form-grid">
        <label>绿线<input name="green_threshold" type="number" step="0.01" value="${s.green_threshold}" /></label>
        <label>蓝线<input name="blue_threshold" type="number" step="0.01" value="${s.blue_threshold}" /></label>
        <label>课件页权重<input name="material_page_weight" type="number" step="0.01" value="${s.material_page_weight}" /></label>
        <label>交互权重<input name="material_interaction_weight" type="number" step="0.01" value="${s.material_interaction_weight}" /></label>
        <label>ADG窗口天数<input name="adg_window_days" type="number" value="${s.adg_window_days}" /></label>
        <label>IDG周期天数<input name="idg_period_days" type="number" value="${s.idg_period_days}" /></label>
        <label>期末IDG占比<input name="idg_final_term_weight" type="number" step="0.01" value="${s.idg_final_term_weight}" /></label>
        <label>系数蓝线<input name="coefficient_blue_threshold" type="number" step="0.01" value="${s.coefficient_blue_threshold}" /></label>
        <label>系数红线<input name="coefficient_red_threshold" type="number" step="0.01" value="${s.coefficient_red_threshold}" /></label>
        <label>单页最短停留秒<input name="anti_cheat_min_page_stay_seconds" type="number" value="${s.anti_cheat_min_page_stay_seconds}" /></label>
        <label>刷课判定秒<input name="anti_cheat_rush_seconds" type="number" value="${s.anti_cheat_rush_seconds}" /></label>
        <label>公告完整阅读秒<input name="announcement_min_stay_seconds" type="number" value="${s.announcement_min_stay_seconds}" /></label>
        <label>ADG权重（近→远）<input name="adg_weights_recent_to_old" value="${esc((s.adg_weights_recent_to_old || []).join(','))}" /></label>
        <div style="align-self:end"><button class="button" type="submit">保存</button></div>
      </form>
      <p class="small muted">低利害模式 low_stakes_mode=true 为制度常量，接口层禁止关闭。</p>
    </section>`;
}

function renderUsers() {
  return `
    <section class="card" style="margin-bottom:18px"><h3>新增账号</h3>
      <form id="user-form" class="form-grid">
        <label>姓名<input name="display_name" required /></label>
        <label>登录账号<input name="username" required /></label>
        <label>角色<select name="role"><option value="teacher">教师</option><option value="student">学生</option><option value="admin">管理员</option></select></label>
        <label>初始密码（至少10位）<input name="password" type="password" minlength="10" required /></label>
        <div style="align-self:end"><button class="button" type="submit">创建账号</button></div>
      </form>
    </section>
    <section class="card"><h3>账号与角色</h3>
      <div class="table-wrap"><table><thead><tr><th>姓名</th><th>账号</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody>
      ${state.users.map(user => `<tr>
        <td><input data-user-field="display_name" data-id="${user.id}" value="${esc(user.display_name)}" /></td>
        <td>${esc(user.username)}</td>
        <td><select data-user-field="role" data-id="${user.id}">
          ${['teacher','student','admin'].map(role => `<option value="${role}" ${user.role===role?'selected':''}>${roleName(role)}</option>`).join('')}
        </select></td>
        <td>${user.active ? '<span class="chip green">启用</span>' : '<span class="chip red">停用</span>'}</td>
        <td><button class="button secondary small" data-action="update-user" data-id="${user.id}" data-active="${user.active ? '1' : '0'}">保存</button>
        <button class="button secondary small" data-action="update-user" data-id="${user.id}" data-active="${user.active ? '0' : '1'}">${user.active ? '停用' : '启用'}</button>
        <button class="button secondary small" data-action="reset-user-password" data-id="${user.id}">重置密码</button></td>
      </tr>`).join('')}
      </tbody></table></div>
      <p class="small muted">角色边界：管理员管理参数、账号与审计；教师采集、核定、发布、干预和转介；学生仅查看本人已发布数据并可使用静默通道。</p>
    </section>
  `;
}

function renderManual() {
  const isStudent = state.me.role === 'student';
  const quickSteps = isStudent ? [
    ['1. 登录并修改密码', '首次登录后，在工作台修改初始密码。'],
    ['2. 绑定学号和姓名', '必须与老师导入的班级信息一致，绑定后只能看本人数据。'],
    ['3. 查看我的成长报告', '老师发布后，在“我的成长报告”查看本人 IDG/ADG 和分子分母。'],
    ['4. 看自身趋势', '在“成长曲线”查看自己的基线与变化，不显示班级排名。'],
    ['5. 需要支持时主动求助', '可填写量表或申请静默；紧急情况直接联系老师/辅导员。'],
  ] : [
    ['1. 创建课程', '课程管理中填写课程名、代码、数据源和学年。'],
    ['2. 先导入历史 IDG', '数据采集中选择“历史 IDG 表”，填写历史年份，用于 ADG。'],
    ['3. 再导入即时数据', '选择雨课堂原始工作簿，填写评价日期和班级筛选。'],
    ['4. 生成并检查报表', '报表闭环中生成 IDG/ADG，逐项检查分子分母。'],
    ['5. 核定后发布', '教师核定 → 发布学生端；24 小时内可撤回。'],
    ['6. 处理预警和干预', '预警中心合并显示同一学生多项预警，用下拉表单登记干预。'],
    ['7. 处理工单', '量表与育心中分派并处理 2 小时心理转介工单。'],
  ];
  return `
    <div class="notice">手册按角色编写。教师和学生可先看“快速上手”，再下载或打开完整手册。</div>
    <div class="grid cols-2">
      <section class="card">
        <h3>${isStudent ? '学生快速上手' : '教师快速上手'}</h3>
        ${quickSteps.map(([title, text]) => `
          <div class="callout" style="margin-bottom:10px">
            <strong>${esc(title)}</strong>
            <div class="small muted">${esc(text)}</div>
          </div>
        `).join('')}
      </section>
      <section class="card">
        <h3>完整手册</h3>
        <div class="button-row">
          <a class="button" href="/docs/teacher-guide.md" target="_blank" rel="noopener">教师手册</a>
          <a class="button secondary" href="/docs/student-guide.md" target="_blank" rel="noopener">学生手册</a>
          <a class="button secondary" href="/docs/admin-guide.md" target="_blank" rel="noopener">管理员手册</a>
          <a class="button secondary" href="/docs/faq.md" target="_blank" rel="noopener">常见问题</a>
          <a class="button secondary" href="/docs/README.md" target="_blank" rel="noopener">手册总览</a>
        </div>
        <div class="callout" style="margin-top:14px">
          <strong>课堂投放建议</strong>
          <div class="small muted">给新学生发送“学生使用手册”和平台地址；提醒他们首次登录后绑定学号、姓名，并及时修改密码。</div>
        </div>
        <div class="callout" style="margin-top:10px">
          <strong>教师教研建议</strong>
          <div class="small muted">新老师先用一个班完成：历史导入 → 即时导入 → 生成 → 核定 → 发布 → 预警干预，再推广到多个班。</div>
        </div>
      </section>
    </div>
  `;
}

function renderAudit() {
  return `<section class="card"><h3>审计事件</h3><div class="table-wrap"><table><thead><tr><th>时间</th><th>用户</th><th>动作</th><th>对象</th><th>详情</th></tr></thead><tbody>
    ${state.audit.map(a => `<tr><td>${esc(fmtDateTime(a.created_at))}</td><td>${esc(a.username || '系统')}</td><td>${esc(a.action)}</td><td>${esc(a.object_type)}#${esc(a.object_id)}</td><td class="small">${esc(JSON.stringify(a.detail))}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">暂无</td></tr>'}
  </tbody></table></div></section>`;
}

function render() {
  const app = document.getElementById('app');
  if (!state.me) {
    app.innerHTML = renderLogin();
    return;
  }
  let content = '';
  if (state.view === 'dashboard') content = renderDashboard();
  else if (state.view === 'courses') content = renderCourses();
  else if (state.view === 'imports') content = renderImports();
  else if (state.view === 'reports') content = renderReports();
  else if (state.view === 'warnings') content = renderWarnings();
  else if (state.view === 'growth') content = renderGrowth();
  else if (state.view === 'mind') content = renderMind();
  else if (state.view === 'questionnaire') content = renderQuestionnaire();
  else if (state.view === 'manual') content = renderManual();
  else if (state.view === 'settings') content = renderSettings();
  else if (state.view === 'users') content = renderUsers();
  else if (state.view === 'audit') content = renderAudit();
  app.innerHTML = renderShell(content) + (state.interventionDraft ? interventionModal() : '') + (state.message ? `<div class="toast ${state.message.error ? 'error' : ''}">${esc(state.message.text)}</div>` : '');
}

function interventionModal() {
  const draft = state.interventionDraft;
  return `
    <div style="position:fixed;inset:0;background:rgba(15,23,42,.55);display:grid;place-items:center;padding:20px;z-index:40">
      <section class="card" style="width:min(560px,100%)">
        <h3>登记干预</h3>
        <form id="intervention-form" class="grid">
          <label>干预类型<select name="type" required>
            <option value="navigation|学习提醒">导航仪 · 学习提醒</option>
            <option value="navigation|最小行动目标">导航仪 · 最小行动目标</option>
            <option value="navigation|成长趋势反馈">导航仪 · 成长趋势反馈</option>
            <option value="ambulance|教师一对一">救护车 · 教师一对一</option>
            <option value="ambulance|心理辅导转介">救护车 · 心理辅导转介</option>
          </select></label>
          <label>具体安排/记录<textarea name="content" required placeholder="例如：48小时内联系学生，确认公告阅读困难并约定最小行动目标。"></textarea></label>
          <label class="small"><input type="checkbox" name="create_referral" style="width:auto" /> 同步创建 2 小时心理转介工单</label>
          <div class="button-row">
            <button class="button" type="submit">保存并留痕</button>
            <button class="button secondary" type="button" data-action="close-intervention">取消</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

async function openReport(id) {
  try {
    state.currentReport = await api(`/api/reports/${id}`);
    state.view = 'reports';
    render();
  } catch (error) { toast(error.message, true); }
}

async function openQuestionnaire(id) {
  try {
    state.questionnaire = await api(`/api/questionnaires/${id}`);
    state.questionnaireAnswers = {};
    state.view = 'questionnaire';
    render();
  } catch (error) { toast(error.message, true); }
}

async function loadGrowth(studentNo) {
  try {
    state.growthStudentNo = String(studentNo || '');
    state.growth = await api(`/api/students/growth?student_no=${encodeURIComponent(state.growthStudentNo)}`);
    state.view = 'growth';
    render();
  } catch (error) { toast(error.message, true); }
}

async function exportReport() {
  if (!state.currentReport) return;
  const confirmed = window.confirm('导出仅用于低利害诊断与个体支持，不用于排名/评优/问责。是否继续？');
  if (!confirmed) return;
  try {
    const response = await fetch(`/api/reports/${state.currentReport.id}/export?confirm_low_stakes=true`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || '导出失败');
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `report-${state.currentReport.id}-low-stakes.csv`;
    link.click();
    URL.revokeObjectURL(url);
    toast('导出完成，已写入审计');
  } catch (error) { toast(error.message, true); }
}

async function submitForm(event) {
  event.preventDefault();
  const form = event.target;
  const id = form.id;
  const formData = new FormData(form);
  const value = Object.fromEntries(formData.entries());
  try {
    if (id === 'login-form') return;
    if (id === 'binding-form') {
      await api('/api/auth/student-binding', { method: 'POST', json: value });
      state.me = await api('/api/auth/me');
      toast('学号绑定成功');
    } else if (id === 'password-form') {
      const result = await api('/api/auth/change-password', { method: 'POST', json: value });
      form.reset();
      toast(result.message);
    } else if (id === 'course-form') {
      await api('/api/courses', { method: 'POST', json: value });
      await refreshView(true);
      toast('课程已创建');
    } else if (id === 'import-form') {
      const file = formData.get('file');
      if (!file) throw new Error('请选择文件');
      const params = new URLSearchParams({
        course_id: value.course_id,
        kind: value.kind,
        period_date: value.period_date || '',
        history_year: value.history_year || '',
        student_filter: value.student_filter || '',
      });
      const response = await fetch(`/api/imports?${params}`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${state.token}`,
          'X-Filename': encodeURIComponent(file.name),
        },
        body: await file.arrayBuffer(),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '导入失败');
      state.importResult = data;
      await refreshView(true);
      toast(`导入成功：${data.rows_imported} 条`);
    } else if (id === 'report-form') {
      const result = await api('/api/reports/generate', { method: 'POST', json: value });
      await refreshView(true);
      await openReport(result.report_id);
      toast('报表已生成，待教师核定');
      return;
    } else if (id === 'growth-form') {
      await loadGrowth(value.student_no);
      return;
    } else if (id === 'settings-form') {
      value.adg_weights_recent_to_old = String(value.adg_weights_recent_to_old || '').split(',').map(x => Number(x.trim())).filter(x => !Number.isNaN(x));
      state.settings = await api('/api/settings', { method: 'PUT', json: value });
      toast('参数已保存');
    } else if (id === 'questionnaire-form') {
      value.questions = JSON.parse(value.questions || '[]');
      await api('/api/questionnaires', { method: 'POST', json: value });
      await refreshView(true);
      toast('量表已保存');
    } else if (id === 'silent-form') {
      await api('/api/silent-requests', { method: 'POST', json: value });
      await refreshView(true);
      toast('静默申请已提交');
    } else if (id === 'referral-form') {
      await api('/api/referrals', { method: 'POST', json: value });
      await refreshView(true);
      toast('转介工单已创建');
    } else if (id === 'user-form') {
      await api('/api/users', { method: 'POST', json: value });
      await refreshView();
      toast('账号已创建');
    } else if (id === 'intervention-form') {
      const [mode, category] = String(value.type || '').split('|');
      const payload = {
        ...state.interventionDraft,
        mode: mode || 'navigation',
        category: category || '学习提醒',
        content: value.content,
        create_referral: value.create_referral === 'on',
      };
      const result = await api('/api/interventions', { method: 'POST', json: payload });
      state.interventionDraft = null;
      toast(result.referral_id ? `干预已保存，并创建 2 小时转介工单 #${result.referral_id}` : '干预已保存并留痕');
    } else if (id === 'questionnaire-response-form') {
      const answers = Object.keys(state.questionnaireAnswers).reduce((acc, key) => {
        acc[key] = state.questionnaireAnswers[key];
        return acc;
      }, {});
      const result = await api(`/api/questionnaires/${state.questionnaire.id}/responses`, { method: 'POST', json: { answers } });
      state.view = 'mind';
      await refreshView();
      toast(result.message);
      return;
    }
    render();
  } catch (error) {
    toast(error.message, true);
  }
}

document.addEventListener('submit', login);
document.addEventListener('submit', submitForm);

document.addEventListener('click', async event => {
 const button = event.target.closest('[data-action]');
 if (!button) return;
 const action = button.dataset.action;
 try {
   if (action === 'nav') await setView(button.dataset.view);
   else if (action === 'logout') await logout();
   else if (action === 'open-report') await openReport(button.dataset.id);
   else if (action === 'close-report') { state.currentReport = null; await setView('reports'); }
   else if (action === 'open-questionnaire') await openQuestionnaire(button.dataset.id);
   else if (action === 'close-questionnaire') { state.questionnaire = null; await setView('mind'); }
   else if (action === 'choose-option') {
     state.questionnaireAnswers[button.dataset.question] = Number(button.dataset.index);
     render();
   } else if (action === 'growth') await loadGrowth(button.dataset.id);
   else if (action === 'review-report') {
     if (!window.confirm('确认已检查分子/分母证据与低利害用途？')) return;
     await api(`/api/reports/${state.currentReport.id}/review`, { method: 'POST', json: { confirmed: true } });
     await openReport(state.currentReport.id);
   } else if (action === 'publish-report') {
     if (!window.confirm('发布后学生端可见；发布成功率要求 ≥99%，24小时内可撤回。确认发布？')) return;
     await api(`/api/reports/${state.currentReport.id}/publish`, { method: 'POST', json: {} });
     await openReport(state.currentReport.id);
   } else if (action === 'revoke-report') {
     const reason = window.prompt('请输入撤回原因：');
     if (reason === null) return;
     await api(`/api/reports/${state.currentReport.id}/revoke`, { method: 'POST', json: { reason } });
     await openReport(state.currentReport.id);
   } else if (action === 'export-report') await exportReport();
   else if (action === 'close-intervention') {
     state.interventionDraft = null;
     render();
   }
   else if (action === 'intervention') {
     state.interventionDraft = {
      report_item_id: Number(button.dataset.id || 0) || null,
      student_id: Number(button.dataset.student || 0),
      course_id: Number(button.dataset.course || 0),
      student_no: button.dataset.studentNo || '',
     };
     render();
   } else if (action === 'delete-import') {
     if (!window.confirm('确认删除这条导入记录？若存在同课程同类型的重复导入，数据会自动挂到另一条记录，不会重复保留。')) return;
     await api(`/api/imports/${button.dataset.id}`, { method: 'DELETE' });
     await refreshView();
     toast('导入记录已删除');
   } else if (action === 'update-referral') {
     const row = button.closest('tr');
     await api('/api/referrals', { method: 'PATCH', json: {
       id: Number(button.dataset.id),
       status: row.querySelector('[data-referral-field="status"]').value,
       notes: row.querySelector('[data-referral-field="notes"]').value,
     }});
     await refreshView();
     toast('转介工单已更新');
   } else if (action === 'update-user') {
     const row = button.closest('tr');
     await api('/api/users', { method: 'PATCH', json: {
       id: Number(button.dataset.id),
       display_name: row.querySelector('[data-user-field="display_name"]').value,
       role: row.querySelector('[data-user-field="role"]').value,
       active: button.dataset.active === '1',
     }});
     await refreshView();
     toast('账号已更新');
   } else if (action === 'reset-user-password') {
     const password = window.prompt('请输入新密码（至少10位）：');
     if (!password) return;
     await api('/api/users', { method: 'PATCH', json: { id: Number(button.dataset.id), new_password: password } });
     toast('密码已重置');
   } else if (action === 'silent-decide') {
     await api('/api/silent-requests', { method: 'PATCH', json: { id: Number(button.dataset.id), status: button.dataset.status } });
     await refreshView();
     toast('静默申请已处理');
   }
 } catch (error) { toast(error.message, true); }
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.interventionDraft) {
    state.interventionDraft = null;
    render();
  }
});

async function boot() {
  if (state.token) {
    try {
      state.me = await api('/api/auth/me');
      await refreshView(true);
    } catch (_) {
      state.token = '';
      localStorage.removeItem('dds_token');
    }
  }
  render();
}

boot();
