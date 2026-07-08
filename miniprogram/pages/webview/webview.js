// ─────────────────────────────────────────────
// web-view 页：加载 H5 前端，token 拼在 URL 上传入
// H5 侧（index.html）会从 URL query 读取 token 并存入 localStorage
// ─────────────────────────────────────────────
const app = getApp()

Page({
  data: {
    src: '',
  },

  onLoad() {
    const base = app.globalData.apiBase
    const token = app.globalData.token || wx.getStorageSync('yj_token') || ''

    if (!token) {
      // 没有 token（异常路径），回登录页重新走一遍
      wx.redirectTo({ url: '/pages/index/index' })
      return
    }

    const name = encodeURIComponent(app.globalData.userName || '微信用户')
    this.setData({
      src: `${base}/app?token=${encodeURIComponent(token)}&name=${name}`,
    })
  },

  // web-view 加载失败（域名不合法/网络问题）时提示
  onWebviewError(e) {
    console.error('[web-view 加载失败]', e.detail)
    wx.showModal({
      title: '页面加载失败',
      content: '请确认已打开调试模式（真机：右上角"···"→ 打开调试），或稍后重试',
      showCancel: false,
    })
  },
})
