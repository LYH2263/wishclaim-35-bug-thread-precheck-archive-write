<template>
  <div class="wall">
    <h1 class="serif">愿望墙</h1>
    <p class="tag">无顶栏 · 瀑布流 · 点卡片认领</p>
    <div class="masonry">
      <article v-for="w in rows" :key="w.id" class="card" @click="$router.push('/wishes/'+w.id)">
        <h3>{{ w.title || '（无标题）' }}</h3>
        <p>{{ w.note }}</p>
        <span class="tag">{{ w.status }} · {{ w.data_quality }} · {{ threadTag(w) }} · 留言 {{ w.comment_count || 0 }} 楼</span>
      </article>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
// Same rule as the backend write gate: archived is permanent, a live claim
// freezes the thread, anything else stays appendable.
const threadTag = (w) =>
  w.status === 'fulfilled' ? '已归档只读' : w.status === 'claimed' ? '留言冻结' : '可追加'
onMounted(async () => { rows.value = await api('/wishes') })
</script>
