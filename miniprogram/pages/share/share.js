// ─────────────────────────────────────────────
// 分享页 — 用户点击"分享"后跳转此页，通过 open-type="share" 触发小程序转发
// ─────────────────────────────────────────────
const app = getApp();

Page({
  data: {
    userId: '',
  },

  onLoad() {
    const user = app.globalData.userInfo || {};
    this.setData({ userId: user.user_id || '' });
  },

  // 转发给朋友
  onShareAppMessage() {
    const user = app.globalData.userInfo || {};
    const userId = user.user_id || '';
    return {
      title: '用 AI 重现你珍视的人，随时对话',
      path: userId ? `/pages/login/login?ref=${userId}` : '/pages/login/login',
    };
  },

  // 分享到朋友圈（需要在 app.json 开启 "permission" 或在 page.json 配置）
  onShareTimeline() {
    const user = app.globalData.userInfo || {};
    const userId = user.user_id || '';
    return {
      title: '言己 — 让 AI 替身陪你说说话',
      query: userId ? `ref=${userId}` : '',
    };
  },

  goBack() {
    wx.navigateBack({ delta: 1 });
  },
});
