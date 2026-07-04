// ─────────────────────────────────────────────
// 首页 — web-view 加载主应用
// ─────────────────────────────────────────────
const app = getApp();

Page({
  data: {
    webviewUrl: '',
  },

  onLoad() {
    const token = app.globalData.token;
    if (!token) {
      // token 不存在或已过期，回登录页
      wx.redirectTo({ url: '/pages/login/login' });
      return;
    }

    const user    = app.globalData.userInfo || {};
    const name    = encodeURIComponent(user.display_name || '用户');
    const baseUrl = app.globalData.apiBase;
    const url     = `${baseUrl}/app?token=${token}&name=${name}`;

    this.setData({ webviewUrl: url });
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
