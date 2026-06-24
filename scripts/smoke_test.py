#!/usr/bin/env python3
"""
端到端冒烟测试（无需外部 API Key）
测试：注册 → 登录 → 创建替身 → 添加记忆 → 敏感话题预设 → 对话拦截

运行：
  # 先启动服务: python3 backend/app.py
  # 另一个终端: python3 scripts/smoke_test.py
"""
import sys, os, json, urllib.request, urllib.error, urllib.parse

BASE = "http://localhost:5000"

def req(method, path, body=None, token=None):
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def run():
    print("=== 替身 API 冒烟测试 ===\n")

    # 1. 注册
    print("1. 注册用户...")
    code, data = req("POST", "/api/auth/register", {
        "display_name": "小王",
        "phone": "13800000001",
        "password": "test123"
    })
    assert code == 201, f"注册失败: {data}"
    token = data["token"]
    print(f"   ✅ 注册成功，token: {token[:20]}...")

    # 2. 登录
    print("2. 登录...")
    code, data = req("POST", "/api/auth/login", {
        "phone": "13800000001",
        "password": "test123"
    })
    assert code == 200, f"登录失败: {data}"
    token = data["token"]
    print(f"   ✅ 登录成功")

    # 3. 创建替身
    print("3. 创建替身（儿子角色）...")
    code, data = req("POST", "/api/avatars", {
        "name": "小王（儿子）",
        "relationship": "son"
    }, token)
    assert code == 201, f"创建失败: {data}"
    avatar_id = data["id"]
    share_code = data["share_code"]
    print(f"   ✅ 替身创建成功 ID: {avatar_id[:8]}... 共享码: {share_code}")

    # 4. 添加记忆
    print("4. 添加观点记忆...")
    code, data = req("POST", f"/api/memories/{avatar_id}", {
        "content": "我觉得人工智能会改变很多行业，但也会带来一些失业问题，需要社会配套跟上。",
        "mem_type": "opinion",
        "topic_tags": ["科技", "AI"]
    }, token)
    assert code == 201, f"添加记忆失败: {data}"
    print(f"   ✅ 记忆添加成功")

    # 5. 添加生活记忆
    code, _ = req("POST", f"/api/memories/{avatar_id}", {
        "content": "爸，你还记得吗？小时候你带我去钓鱼，我还把鱼竿掉水里了，哈哈",
        "mem_type": "episode",
        "topic_tags": ["回忆", "童年"]
    }, token)

    # 6. 添加说话风格
    code, _ = req("POST", f"/api/memories/{avatar_id}", {
        "content": "行，知道了，你别担心我，我这里挺好的。",
        "mem_type": "style",
    }, token)

    # 7. 添加敏感话题预设
    print("5. 添加敏感话题预设...")
    code, data = req("POST", f"/api/avatars/{avatar_id}/topics", {
        "trigger_keywords": ["工资", "挣多少", "赚多少"],
        "strategy": "deflect"
    }, token)
    assert code == 201, f"添加话题失败: {data}"
    code, data = req("POST", f"/api/avatars/{avatar_id}/topics", {
        "trigger_keywords": ["什么时候回来", "回家"],
        "strategy": "preset_answer",
        "preset_content": "快了快了，过年肯定回去，你别担心！"
    }, token)
    print(f"   ✅ 敏感话题预设成功")

    # 8. 对话测试（创建者自测）
    print("6. 对话测试...")

    # 8a. 普通对话（会调用 LLM，如没配置 Key 会报错）
    code, data = req("POST", f"/api/chat/{avatar_id}", {
        "message": "爸，你最近身体咋样"
    }, token)
    if code == 503:
        print(f"   ⚠️  LLM 未配置 ({data.get('error','')[:50]}...)，跳过普通对话测试")
    elif code == 200:
        print(f"   ✅ 普通对话回复: {data['reply'][:50]}...")
    else:
        print(f"   ❌ 对话失败 {code}: {data}")

    # 8b. 敏感话题拦截测试（不需要 LLM）
    code, data = req("POST", f"/api/chat/{avatar_id}", {
        "message": "你现在工资多少啊"
    }, token)
    assert code == 200, f"对话失败: {data}"
    assert data.get("intercepted") == True, "应该被拦截"
    print(f"   ✅ 敏感话题拦截成功: {data['reply']}")

    # 8c. 预设答案测试
    code, data = req("POST", f"/api/chat/{avatar_id}", {
        "message": "你什么时候回来啊"
    }, token)
    assert code == 200
    assert data.get("intercepted") == True
    print(f"   ✅ 预设答案返回: {data['reply']}")

    # 8d. 安全拦截（借钱）
    code, data = req("POST", f"/api/chat/{avatar_id}", {
        "message": "你能不能转账给我500"
    }, token)
    assert code == 200
    assert data.get("intercepted") == True
    assert data.get("strategy") == "safety_block"
    print(f"   ✅ 安全拦截成功: {data['reply']}")

    # 9. 查看历史
    print("7. 对话历史...")
    code, data = req("GET", f"/api/chat/{avatar_id}/history", token=token)
    assert code == 200
    print(f"   ✅ 历史记录 {len(data)} 条")

    print("\n=== ✅ 所有测试通过！===")
    print(f"\n下一步：")
    print(f"  1. 在 .env 中填入 ANTHROPIC_API_KEY")
    print(f"  2. 上传微信聊天记录: POST /api/ingest/{avatar_id}/wechat")
    print(f"  3. 生成共享码发给接收者: {share_code}")

if __name__ == "__main__":
    run()
