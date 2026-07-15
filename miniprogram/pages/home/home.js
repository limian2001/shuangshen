// ─────────────────────────────────────────────
// 首页 — 未登录：原生落地页  |  已登录：web-view 主应用
// ─────────────────────────────────────────────
const app = getApp();

Page({
  data: {
    isLoggedIn: false,
    webviewUrl: '',
    refCode: '',
  },

  onLoad(options) {
    // 记录分享链接中的邀请码
    if (options && options.ref) {
      app.globalData.pendingRefCode = options.ref;
      this.setData({ refCode: options.ref });
    }

    const token = app.globalData.token;
    if (token) {
      this._loadWebview(options);
    }
    // 无 token：保持 isLoggedIn: false，显示落地页，不强制跳转
  },

  // 从登录页返回后触发，刷新登录状态
  onShow() {
    const token = app.globalData.token;
    if (token && !this.data.isLoggedIn) {
      this._loadWebview({});
    }
  },

  // 构建 webview URL 并切换到已登录状态
  _loadWebview(options) {
    const token = app.globalData.token;
    if (!token) return;

    const ref = (options && options.ref)
      || app.globalData.pendingRefCode
      || this.data.refCode
      || '';
    if (ref) app.globalData.pendingRefCode = '';

    const user    = app.globalData.userInfo || {};
    const name    = encodeURIComponent(user.display_name || '用户');
    const uid     = encodeURIComponent(user.user_id || '');
    const baseUrl = app.globalData.apiBase;
    const refParam = ref ? `&ref=${encodeURIComponent(ref)}` : '';
    const url = `${baseUrl}/app?token=${token}&name=${name}&uid=${uid}${refParam}`;

    this.setData({ isLoggedIn: true, webviewUrl: url });
  },

  // 落地页"立即体验"按钮
  goLogin() {
    wx.navigateTo({ url: '/pages/login/login' });
  },

  // 转发给朋友
  onShareAppMessage() {
    const user   = app.globalData.userInfo || {};
    const userId = user.user_id || '';
    return {
      title: '用 AI 重现你珍视的人，随时对话',
      path:  userId ? `/pages/home/home?ref=${userId}` : '/pages/home/home',
    };
  },

  // 分享到朋友圈
  onShareTimeline() {
    const user   = app.globalData.userInfo || {};
    const userId = user.user_id || '';
    return {
      title: '言己 — 让 AI 替身陪你说说话',
      query: userId ? `ref=${userId}` : '',
    };
  },

  // web-view 消息回调（token 过期时退出登录，回到落地页）
  onWebViewMessage(e) {
    const msg = e.detail.data && e.detail.data[e.detail.data.length - 1];
    if (!msg) return;
    if (msg.type === 'auth_expired') {
      app.clearAuth();
      this.setData({ isLoggedIn: false, webviewUrl: '' });
    }
  },
});
