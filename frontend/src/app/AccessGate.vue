<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, ApiError } from '@/shared/api/client'

const ready = ref(false)
const loading = ref(true)
const key = ref('')
const error = ref('')
const suggestions = ref<string[]>([])
const sessionRequired = ref(false)
const router = useRouter()

function showError(cause: unknown) {
  error.value = cause instanceof ApiError ? `${cause.title}：${cause.detail}` : String(cause)
  suggestions.value = cause instanceof ApiError ? cause.suggestions : ['检查后端服务是否已经启动']
}

async function check() {
  loading.value = true
  error.value = ''
  try {
    const status = await api.get<{ authenticated: boolean; required: boolean }>('/auth/status')
    sessionRequired.value = status.required
    ready.value = status.authenticated
  } catch (cause) {
    showError(cause)
  } finally {
    loading.value = false
  }
}

async function login() {
  loading.value = true
  error.value = ''
  try {
    await api.post('/auth/session', { key: key.value.trim() })
    key.value = ''
    // 检查 Cookie 确实被浏览器保存，避免 HTTP 部署下出现登录成功却不能访问。
    const status = await api.get<{ authenticated: boolean }>('/auth/status')
    if (!status.authenticated) throw new Error('登录会话未保存。服务部署请使用 HTTPS，或由管理员配置本地 HTTP 会话。')
    ready.value = true
  } catch (cause) {
    showError(cause)
  } finally {
    loading.value = false
  }
}

function expired() {
  if (!ready.value) return
  ready.value = false
  error.value = '访问凭证已过期或被撤销，请重新登录。'
}

async function logout() {
  if (router.currentRoute.value.name !== 'projects') {
    const stopped = await router.push({ name: 'projects' })
    if (stopped) return // 编辑器的离开守卫先保存草稿，失败则留在原处。
  }
  await api.del('/auth/session')
  ready.value = false
  error.value = ''
}

onMounted(() => { void check(); window.addEventListener('aivs:unauthorized', expired) })
onUnmounted(() => window.removeEventListener('aivs:unauthorized', expired))
</script>

<template>
  <template v-if="ready">
    <slot />
    <p v-if="error" role="alert" class="access-live-error">{{ error }} {{ suggestions.join('；') }}</p>
    <button v-if="sessionRequired" class="access-exit" title="结束当前访问会话" @click="logout().catch(showError)">退出访问</button>
  </template>
  <main v-else class="access-gate">
    <form class="access-card" @submit.prevent="login">
      <span class="access-eyebrow">AI VIDEO STUDIO</span>
      <h1>连接你的创作工作台</h1>
      <p>服务部署需要访问 Key。请向部署管理员获取有权访问项目的凭证。</p>
      <label for="access-key">访问 Key</label>
      <input id="access-key" v-model="key" type="password" autocomplete="off" placeholder="aivs_…" :disabled="loading" required />
      <p v-if="error" class="access-error" role="alert">{{ error }}</p>
      <ul v-if="error && suggestions.length"><li v-for="item in suggestions" :key="item">{{ item }}</li></ul>
      <button type="submit" :disabled="loading || !key.trim()">{{ loading ? '正在连接…' : '进入工作台' }}</button>
      <button type="button" class="access-retry" :disabled="loading" @click="check">重新检查本机连接</button>
    </form>
  </main>
</template>

<style scoped>
.access-gate { min-height: 100vh; display: grid; place-items: center; padding: 32px; background: var(--color-base-0); color: var(--color-fg-1); }
.access-card { width: min(440px, 100%); padding: 36px; border: 1px solid var(--color-line-2); border-radius: 16px; background: var(--color-base-1); display: grid; gap: 16px; }
.access-eyebrow { font-size: 11px; letter-spacing: .18em; color: var(--color-accent); }
h1 { font-size: 24px; margin: 0; }
p, li { font-size: 13px; line-height: 1.7; color: #a9b0bf; margin: 0; }
label { font-size: 13px; }
input { padding: 12px; border: 1px solid #454c61; border-radius: 6px; background: #11141c; color: #fff; }
button { padding: 11px; border: 0; border-radius: 6px; background: var(--color-accent); color: var(--color-base-0); cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
.access-retry { background: transparent; color: #b4bce4; }
.access-error { color: #ffb4ae; }
.access-exit { position: fixed; bottom: 3px; right: 12px; z-index: 50; padding: 2px 7px; font-size: 10px; background: #252a38; }
.access-live-error { position: fixed; top: 44px; right: 16px; z-index: 100; max-width: 480px; padding: 12px; background: #39272c; color: #ffb4ae; border-radius: 6px; }
</style>
