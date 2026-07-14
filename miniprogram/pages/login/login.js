// ─────────────────────────────────────────────
// 登录页 — 两步登录：openid 静默检查 → 手机号授权
// ─────────────────────────────────────────────
const app = getApp();
// 兜底：即使 globalData 传递异常，也能正确拼接 URL
const API_BASE = (app && app.globalData && app.globalData.apiBase) || 'https://app.mianmianlife.com';

Page({
  data: {
    loading: false,
    step: 'checking',  // 'checking' | 'need_phone' | 'done'
    nickname: '',
    wxCode: '',        // 缓存 wx.login() 的 code（手机授权步骤用）
    agreed: false,     // 是否已勾选隐私协议
  },

  onLoad() {
    if (app.globalData.token) {
      this._goHome();
      return;
    }
    this._doSilentLogin();
  },

  // ① 静默登录：wx.login() → 检查 openid
  _doSilentLogin() {
    this.setData({ loading: true, step: 'checking' });
    wx.login({
      success: (res) => {
        if (!res.code) {
          this._showError('获取登录凭证失败');
          return;
        }
        const wxCode = res.code;
        this.setData({ wxCode });
        wx.request({
          url: API_BASE + '/api/auth/wechat',
          method: 'POST',
          data: { code: wxCode },
          success: (r) => {
            if (r.statusCode === 200 && r.data) {
              if (r.data.token) {
                this._saveAndGo(r.data);
              } else if (r.data.need_phone) {
                this.setData({ loading: false, step: 'need_phone' });
              } else {
                this._showError(r.data.error || '登录失败');
              }
            } else {
              this._showError((r.data && r.data.error) || '登录失败，请重试');
            }
          },
          fail: () => this._showError('网络错误，请检查网络后重试'),
        });
      },
      fail: () => this._showError('微信登录失败，请重试'),
    });
  },

  // ② 用户点击"授权手机号"按钮（open-type="getPhoneNumber"）
  onGetPhoneNumber(e) {
    if (e.detail.errno !== undefined && e.detail.errno !== 0) {
      // 用户拒绝授权
      wx.showToast({ title: '需要授权手机号才能使用', icon: 'none', duration: 2000 });
      return;
    }
    const phoneCode = e.detail.code;
    if (!phoneCode) {
      wx.showToast({ title: '获取手机号失败，请重试', icon: 'none', duration: 2000 });
      return;
    }

    const displayName = this.data.nickname.trim() || '言己用户';
    this.setData({ loading: true });

    // 如果 wx_code 已过期（超过5分钟），重新获取
    wx.login({
      success: (res) => {
        const freshCode = res.code || this.data.wxCode;
        wx.request({
          url: API_BASE + '/api/auth/wechat_phone',
          method: 'POST',
          data: {
            wx_code: freshCode,
            phone_code: phoneCode,
            display_name: displayName,
          },
          success: (r) => {
            if (r.statusCode === 200 && r.data && r.data.token) {
              this._saveAndGo(r.data);
            } else {
              this._showError((r.data && r.data.error) || '登录失败，请重试');
            }
          },
          fail: () => this._showError('网络错误，请检查网络后重试'),
        });
      },
      fail: () => {
        // wx.login 失败时用缓存 code 兜底
        wx.request({
          url: API_BASE + '/api/auth/wechat_phone',
          method: 'POST',
          data: {
            wx_code: this.data.wxCode,
            phone_code: phoneCode,
            display_name: displayName,
          },
          success: (r) => {
            if (r.statusCode === 200 && r.data && r.data.token) {
              this._saveAndGo(r.data);
            } else {
              this._showError((r.data && r.data.error) || '登录失败，请重试');
            }
          },
          fail: () => this._showError('网络错误，请检查网络后重试'),
        });
      },
    });
  },

  // 协议勾选变化
  onAgreeChange(e) {
    this.setData({ agreed: e.detail.value.length > 0 });
  },

  // 打开用户协议
  openTerms() {
    wx.navigateTo({
      url: `/pages/policy/policy?type=terms&title=${encodeURIComponent('用户协议')}`,
    });
  },

  // 打开隐私政策
  openPrivacy() {
    wx.navigateTo({
      url: `/pages/policy/policy?type=privacy&title=${encodeURIComponent('隐私政策')}`,
    });
  },

  // nickname input 变化
  onNicknameInput(e) {
    this.setData({ nickname: e.detail.value });
  },

  // 兜底：手动重试按钮
  onTapRetry() {
    this._doSilentLogin();
  },

  _saveAndGo(data) {
    const { token, user_id, display_name } = data;
    app.saveAuth(token, { user_id, display_name });
    this._goHome();
  },

  _showError(msg) {
    this.setData({ loading: false, step: 'need_phone' });
    wx.showToast({ title: msg, icon: 'none', duration: 2500 });
  },

  _goHome() {
    wx.redirectTo({ url: '/pages/home/home' });
  },
});
