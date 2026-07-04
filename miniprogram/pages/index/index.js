// ─────────────────────────────────────────────
// 启动页：静默登录 → 跳转 web-view
// 流程：wx.login() 拿 code → POST /api/auth/wechat → JWT → webview 页
// 每次启动都重新登录（wx.login 是静默的，无需用户操作），
// 保证 token 永远新鲜（后端 JWT 有效期 72h），H5 里不会遇到过期。
// ─────────────────────────────────────────────
const app = getApp()

Page({
  data: {
    status: 'loading',   // loading | error
    errMsg: '',
  },

  onLoad() {
    this.doLogin()
  },

  doLogin() {
    this.setData({ status: 'loading', errMsg: '' })

    wx.login({
      success: (res) => {
        if (!res.code) return this.fail('wx.login 未返回 code')

        wx.request({
          url: `${app.globalData.BASE_URL}/api/auth/wechat`,
          method: 'POST',
          data: { code: res.code },
          timeout: 15000,
          success: (r) => {
            const d = r.data || {}
            if (r.statusCode === 200 && d.token) {
              app.globalData.token = d.token
              app.globalData.userName = d.display_name || '微信用户'
              wx.setStorageSync('ss_token', d.token)
              // redirectTo：不留返回栈，用户按返回直接退出小程序而不是回到登录页
              wx.redirectTo({ url: '/pages/webview/webview' })
            } else {
              this.fail(d.error || `登录失败（HTTP ${r.statusCode}）`)
            }
          },
          fail: (e) => this.fail(`无法连接服务器：${e.errMsg || '网络错误'}`),
        })
      },
      fail: (e) => this.fail(`微信登录失败：${e.errMsg || '未知错误'}`),
    })
  },

  fail(msg) {
    console.error('[登录失败]', msg)
    this.setData({ status: 'error', errMsg: msg })
  },

  onRetry() {
    this.doLogin()
  },
})
