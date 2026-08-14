<template>
  <el-container class="app-shell">
    <el-header class="app-header">
      <div class="brand">
        <el-icon :size="22"><DataAnalysis /></el-icon>
        <span>AI 智能评标系统</span>
      </div>

      <el-menu
        mode="horizontal"
        :default-active="route.path"
        :ellipsis="false"
        router
        class="app-menu"
        background-color="#1f3864"
        text-color="#d7dee9"
        active-text-color="#ffffff"
      >
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          {{ m.title }}
        </el-menu-item>
      </el-menu>

      <div class="header-right">
        <!-- 通知铃铛：未读红点 + 抽屉 -->
        <el-badge :value="notif.unread" :hidden="notif.unread === 0" :max="99" class="bell-badge">
          <el-button :icon="Bell" circle aria-label="通知" @click="openNotif" />
        </el-badge>

        <el-dropdown @command="onCommand">
          <span class="user-chip">
            {{ auth.displayName || auth.user?.username }}
            <el-tag size="small" effect="plain" class="role-tag">{{ roleLabel }}</el-tag>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </el-header>

    <el-main class="app-main">
      <router-view />
    </el-main>

    <!-- 通知抽屉 -->
    <el-drawer v-model="notifDrawer" title="通知" size="380px">
      <template v-if="notif.list.length">
        <div v-for="n in notif.list" :key="n.id" class="notif-item" :class="{ unread: !n.is_read }">
          <div class="notif-title">
            <el-tag size="small" effect="plain">{{ n.type }}</el-tag>
            <span>{{ n.title }}</span>
            <el-button v-if="!n.is_read" link type="primary" size="small" @click="notif.markRead(n.id)">
              标为已读
            </el-button>
          </div>
          <div class="notif-content">{{ n.content }}</div>
          <div class="notif-time">{{ n.created_at }}</div>
        </div>
        <div class="notif-footer">
          <el-button link @click="notif.markAllRead()">全部已读</el-button>
        </div>
      </template>
      <el-empty v-else description="暂无通知" />
    </el-drawer>
  </el-container>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Bell } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
import { useNotificationStore } from '../stores/notification'

const route = useRoute()
const auth = useAuthStore()
const notif = useNotificationStore()

// 各角色菜单（对应 P6.2-P6.5 页面；未实现页面路由已指向占位视图）
const MENUS = {
  ADMIN: [
    { path: '/admin', title: '工作台' },
    { path: '/admin/users', title: '用户管理' },
    { path: '/admin/experts', title: '专家管理' },
    { path: '/admin/suppliers', title: '供应商管理' },
    { path: '/admin/conflicts', title: '工商信息' },
    { path: '/admin/config', title: '系统配置' },
  ],
  PROJECT_MANAGER: [
    { path: '/pm', title: '工作台' },
    { path: '/pm/projects', title: '项目管理' },
    { path: '/pm/tasks', title: '围串标待办' },
    { path: '/pm/reviews', title: '评审进度' },
    { path: '/pm/summary', title: '评标汇总' },
  ],
  REVIEW_EXPERT: [
    { path: '/expert', title: '工作台' },
    { path: '/expert/tasks', title: '我的任务' },
    { path: '/expert/declarations', title: '回避申报' },
    { path: '/expert/history', title: '评审历史' },
  ],
  SUPPLIER: [
    { path: '/supplier', title: '招标市场' },
    { path: '/supplier/bids', title: '投标结果' },
  ],
}

const ROLE_LABEL = {
  ADMIN: '管理员',
  PROJECT_MANAGER: '项目经理',
  REVIEW_EXPERT: '评审专家',
  SUPPLIER: '供应商',
}

const menus = computed(() => MENUS[auth.role] || [])
const roleLabel = computed(() => ROLE_LABEL[auth.role] || auth.role)

const notifDrawer = ref(false)

function openNotif() {
  notifDrawer.value = true
  notif.list()
}

function onCommand(cmd) {
  if (cmd === 'logout') {
    auth.logout()
    window.location.href = '/login'
  }
}
</script>

<style scoped>
.app-shell {
  height: 100vh;
}
.app-header {
  display: flex;
  align-items: center;
  gap: 24px;
  background: #1f3864;
  border-bottom: 1px solid #172e52;
  padding: 0 20px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 16px;
  white-space: nowrap;
  color: #fff;
}
.app-menu {
  flex: 1;
  border-bottom: none;
}
.app-menu.el-menu--horizontal .el-menu-item {
  color: #d7dee9;
}
.app-menu.el-menu--horizontal .el-menu-item:hover {
  background: #24457a;
  color: #fff;
}
.app-menu.el-menu--horizontal .el-menu-item.is-active {
  color: #fff;
  background: #24457a;
  border-bottom: 2px solid #8f9cb1;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}
.bell-badge {
  cursor: pointer;
}
.user-chip {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #fff;
}
.role-tag {
  margin-left: 2px;
  color: #d7dee9;
  border-color: #8f9cb1;
  background: transparent;
}
.app-main {
  background: var(--el-fill-color-lighter);
  padding: 20px;
  overflow: auto;
}
.notif-item {
  padding: 10px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.notif-item.unread .notif-title span {
  font-weight: 600;
}
.notif-title {
  display: flex;
  align-items: center;
  gap: 6px;
}
.notif-title span {
  flex: 1;
  font-size: 14px;
}
.notif-content {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  margin-top: 4px;
  line-height: 1.5;
}
.notif-time {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
  margin-top: 4px;
}
.notif-footer {
  text-align: center;
  padding-top: 12px;
}
</style>
