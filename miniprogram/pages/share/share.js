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

  // 转发给朋友（好友点开落到本页 —— 对新用户友好的着陆页）
  onShareAppMessage() {
    const user = app.globalData.userInfo || {};
    const userId = user.user_id || '';
    return {
      title: '把说不出口的事写下来，它会认真给你回信',
      path: userId ? `/pages/share/share?ref=${userId}` : '/pages/share/share',
    };
  },

  // 分享到朋友圈
  // ⚠️ 朋友圈分享只能落在「当前页面」，且含 web-view 的页面不支持分享朋友圈，
  //    所以必须由本页（纯原生）承担，页面内容也要写给新用户看
  onShareTimeline() {
    const user = app.globalData.userInfo || {};
    const userId = user.user_id || '';
    return {
      title: '解忧杂货店：把说不出口的事写下来，它会认真给你回信',
      query: userId ? `ref=${userId}` : '',
    };
  },

  goBack() {
    wx.navigateBack({ delta: 1 });
  },
});
