<template>
  <el-empty :description="`「${title}」页面开发中（P6 进行中）`">
    <el-button type="primary" @click="$router.back()">返回</el-button>
  </el-empty>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => route.meta.title || '功能')
</script>
