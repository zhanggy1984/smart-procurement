<template>
  <div class="page-header">
    <div>
      <h2 class="page-header-title">{{ title }}</h2>
      <p v-if="desc" class="page-header-desc">{{ desc }}</p>
    </div>
    <div v-if="$slots.default" class="page-header-actions">
      <slot />
    </div>
  </div>
</template>

<script setup>
// 通用页头：标题 + 副标题 + 操作按钮区（默认插槽）。
// 替代各业务页散落的 .toolbar/标题写法，统一排版层级。
defineProps({
  title: { type: String, required: true },
  desc: { type: String, default: '' },
})
</script>
