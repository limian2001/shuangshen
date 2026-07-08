const app = getApp();

Page({
  data: { url: '' },

  onLoad(options) {
    const type  = options.type  || 'privacy';
    const title = decodeURIComponent(options.title || '');
    wx.setNavigationBarTitle({ title });
    this.setData({
      url: `${app.globalData.apiBase}/${type}`,
    });
  },
});
