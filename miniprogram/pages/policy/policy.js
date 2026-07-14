const app = getApp();
const API_BASE = (app && app.globalData && app.globalData.apiBase) || 'https://app.mianmianlife.com';

Page({
  data: { url: '' },

  onLoad(options) {
    const type  = options.type  || 'privacy';
    const title = decodeURIComponent(options.title || '');
    wx.setNavigationBarTitle({ title });
    this.setData({
      url: `${API_BASE}/${type}`,
    });
  },
});
