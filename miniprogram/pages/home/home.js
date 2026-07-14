// ─────────────────────────────────────────────
// 首页 — web-view 加载主应用
// ─────────────────────────────────────────────
const app = getApp();

Page({
  data: {
    webviewUrl: '',
    refCode: '',
  },

  onLoad(options) {
    const token = app.globalData.token;
    if (!token) {
      // token 不存在或已过期，带上 ref 参数回登录页
      const ref = (options && options.ref) || '';
      const url = ref ? `/pages/login/login?ref=${ref}` : '/pages/login/login';
      wx.redirectTo({ url });
      return;
    }

    // 读取邀请码（来自分享链接或登录页透传）
    const ref = (options && options.ref) || app.globalData.pendingRefCode || '';
    if (ref) {
      this.setData({ refCode: ref });
      app.globalData.pendingRefCode = '';  // 消费后清空
    }

    const user    = app.globalData.userInfo || {};
    const name    = encodeURIComponent(user.display_name || '用户');
    const baseUrl = app.globalData.apiBase;
    // ref 传入 webview，H5 页面读取后自动填入邀请码
    const refParam = ref ? `&ref=${encodeURIComponent(ref)}` : '';
    const url     = `${baseUrl}/app?token=${token}&name=${name}${refParam}`;

    this.setData({ webviewUrl: url });
  },

  // 转发给朋友（定义此函数后右上角菜单的"转发"按钮才会亮起）
  onShareAppMessage() {
    const user = app.globalData.userInfo || {};
    const userId = user.user_id || '';
    return {
      title: '用 AI 重现你珍视的人，随时对话',
      // 带入邀请码，被邀请者打开小程序时自动识别
      path: userId ? `/pages/login/login?ref=${userId}` : '/pages/login/login',
    };
  },

  // 分享到朋友圈
  onShareTimeline() {
    const user = app.globalData.userInfo || {};
    const userId = user.user_id || '';
    return {
      title: '言己 — 让 AI 替身陪你说说话',
      query: userId ? `ref=${userId}` : '',
    };
  },

  // web-view 向小程序发消息（目前保留，后续可扩展）
  onWebViewMessage(e) {
    const msg = e.detail.data && e.detail.data[e.detail.data.length - 1];
    if (!msg) return;

    // 处理 token 过期事件（web 页面主动通知）
    if (msg.type === 'auth_expired') {
      app.clearAuth();
      wx.redirectTo({ url: '/pages/login/login' });
    }
  },
});
