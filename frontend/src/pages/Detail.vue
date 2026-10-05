<template>
  <div class="wall">
    <h1 class="serif">{{ w.title }}</h1>
    <p>{{ w.note }}</p>
    <p class="tag">状态 {{ w.status }} · 认领人 {{ w.claimer || '—' }}</p>
    <p v-if="err" class="err">{{ err }}</p>
    <input v-model="claimer" placeholder="你的名字" />
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button @click="claim">认领锁定</button>
      <button class="ghost" @click="release">释放</button>
      <button class="ghost" @click="fulfill">核销完成</button>
    </div>

    <h2 class="serif" style="margin-top:28px">留言 · {{ thread.comment_count || 0 }} 楼</h2>

    <div v-if="thread.state === 'frozen'" class="card" style="cursor:default">
      <p class="tag">认领期间留言串已冻结，释放或超时后可继续；历史留言只读。</p>
    </div>
    <div v-else-if="thread.state === 'archived'" class="card" style="cursor:default">
      <p class="tag">已核销归档，留言串永久只读，不可追加或删除。</p>
    </div>
    <form v-else @submit.prevent="submit" class="card" style="cursor:default">
      <input v-model="author" placeholder="留名（必填）" />
      <textarea v-model="content" rows="3" placeholder="追加留言（不能为空）"></textarea>
      <p class="tag">{{ codepoints(content) }}/{{ thread.max_length || 500 }} 字</p>
      <p v-if="previewMsg" :class="previewOk ? 'tag' : 'err'">{{ previewMsg }}</p>
      <div style="display:flex;gap:8px">
        <button type="button" class="ghost" @click="preflight">预检</button>
        <button type="submit">提交楼层</button>
      </div>
    </form>

    <article v-for="m in thread.comments" :key="m.id" class="card" style="cursor:default">
      <h3>#{{ m.floor }} · {{ m.author }}</h3>
      <p class="tag">{{ m.created_at }}</p>
      <p style="white-space:pre-wrap">{{ m.content }}</p>
    </article>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const props = defineProps({ id: String })
const w = ref({})
const claimer = ref('访客')
const err = ref('')
const author = ref('访客')
const content = ref('')
const thread = ref({ comments: [] })
const previewMsg = ref('')
const previewOk = ref(false)

// JS .length counts UTF-16 code units; spread first to match the backend's
// Unicode code-point count (matters for emoji).
const codepoints = (t) => [...(t || '')].length

const REASONS = {
  empty_content: '内容不能为空（含纯空格）',
  too_long: '超出字数上限',
  empty_author: '请填写作者名',
  frozen: '留言串已冻结，释放或超时后才能追加',
  archived: '已归档，永久只读',
  bad_status: '当前状态不可留言',
}

async function load() {
  const [wish, comments] = await Promise.all([
    api('/wishes/' + props.id),
    api('/wishes/' + props.id + '/comments'),
  ])
  w.value = wish
  thread.value = comments
}
async function claim() {
  err.value=''; try { await api('/wishes/'+props.id+'/claim',{method:'POST',body:JSON.stringify({claimer:claimer.value})}); await load() } catch(e){ err.value=e.message }
}
async function release() {
  err.value=''; try { await api('/wishes/'+props.id+'/release',{method:'POST',body:'{}'}); await load() } catch(e){ err.value=e.message }
}
async function fulfill() {
  err.value=''; try { await api('/wishes/'+props.id+'/fulfill',{method:'POST',body:'{}'}); await load() } catch(e){ err.value=e.message }
}
async function preflight() {
  previewMsg.value=''; previewOk.value=false
  try {
    const v = await api('/wishes/'+props.id+'/comments/preview', {
      method: 'POST', body: JSON.stringify({ content: content.value, author: author.value }),
    })
    previewOk.value = v.ok
    previewMsg.value = v.ok ? '预检通过，可以提交' : REASONS[v.reason] || v.reason
  } catch (e) { previewMsg.value = e.message }
}
async function submit() {
  err.value=''; previewMsg.value=''; previewOk.value=false
  try {
    await api('/wishes/'+props.id+'/comments', {
      method: 'POST', body: JSON.stringify({ content: content.value, author: author.value }),
    })
    content.value = ''
    await load()
  } catch (e) {
    err.value = REASONS[e.message] || e.message
    // State may have changed between preview and submit (TOCTOU) — resync.
    await load()
  }
}
onMounted(load)
</script>
