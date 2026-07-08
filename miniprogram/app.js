// ─────────────────────────────────────────────
// 言己小程序 — 全局入口
// ─────────────────────────────────────────────
App({
  globalData: {
    token: null,
    userInfo: null,
    apiBase: 'https://app.mianmianlife.com',
  },

  onLaunch() {
    this._loadToken();
  },

  // 从本地存储读取 token，检查是否过期
  _loadToken() {
    const token   = wx.getStorageSync('yj_token');
    const expires = wx.getStorageSync('yj_token_expires');
    const user    = wx.getStorageSync('yj_user');

    if (token && expires && Date.now() < expires) {
      this.globalData.token    = token;
      this.globalData.userInfo = user;
    } else {
      this.clearAuth();
    }
  },

  // 保存认证信息（70h，比 JWT 的 72h 提前一点续期）
  saveAuth(token, userInfo) {
    const expires = Date.now() + 70 * 60 * 60 * 1000;
    this.globalData.token    = token;
    this.globalData.userInfo = userInfo;
    wx.setStorageSync('yj_token',         token);
    wx.setStorageSync('yj_user',          userInfo);
    wx.setStorageSync('yj_token_expires', expires);
  },

  clearAuth() {
    this.globalData.token    = null;
    this.globalData.userInfo = null;
    wx.removeStorageSync('yj_token');
    wx.removeStorageSync('yj_user');
    wx.removeStorageSync('yj_token_expires');
  },
});
