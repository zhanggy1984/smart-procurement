import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

// 路由按角色分区；未实现页面先指向 PlaceholderView（P6.x 填充）
const routes = [
  { path: '/login', component: () => import('../views/login/LoginView.vue'), meta: { public: true } },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    children: [
      // ---------- 管理员端（P6.2） ----------
      { path: 'admin', component: () => import('../views/admin/AdminDashboard.vue'), meta: { roles: ['ADMIN'], title: '工作台' } },
      { path: 'admin/users', component: () => import('../views/admin/UserManageView.vue'), meta: { roles: ['ADMIN'], title: '用户管理' } },
      { path: 'admin/experts', component: () => import('../views/admin/ExpertImportView.vue'), meta: { roles: ['ADMIN'], title: '专家管理' } },
      { path: 'admin/suppliers', component: () => import('../views/admin/SupplierImportView.vue'), meta: { roles: ['ADMIN'], title: '供应商管理' } },
      { path: 'admin/conflicts', component: () => import('../views/admin/ConflictImportView.vue'), meta: { roles: ['ADMIN'], title: '工商信息' } },
      { path: 'admin/config', component: () => import('../views/admin/SystemConfigView.vue'), meta: { roles: ['ADMIN'], title: '系统配置' } },
      // ---------- 项目经理端（P6.3） ----------
      { path: 'pm', component: () => import('../views/pm/PmDashboard.vue'), meta: { roles: ['PROJECT_MANAGER'], title: '工作台' } },
      { path: 'pm/projects', component: () => import('../views/pm/ProjectListView.vue'), meta: { roles: ['PROJECT_MANAGER'], title: '项目管理' } },
      { path: 'pm/lots/:lotId/bids', component: () => import('../views/pm/BidListView.vue'), meta: { roles: ['PROJECT_MANAGER'], title: '标书列表' } },
      { path: 'pm/tasks', component: () => import('../views/pm/BidScreenView.vue'), meta: { roles: ['PROJECT_MANAGER'], title: '围串标待办' } },
      { path: 'pm/reviews', component: () => import('../views/pm/PmReviewProgressView.vue'), meta: { roles: ['PROJECT_MANAGER'], title: '评审进度' } },
      { path: 'pm/summary', component: () => import('../views/pm/PmSummaryView.vue'), meta: { roles: ['PROJECT_MANAGER'], title: '评标汇总' } },
      // ---------- 评审专家端（P6.4） ----------
      { path: 'expert', component: () => import('../views/expert/ExpertDashboard.vue'), meta: { roles: ['REVIEW_EXPERT'], title: '工作台' } },
      { path: 'expert/tasks', component: () => import('../views/expert/ExpertTasksView.vue'), meta: { roles: ['REVIEW_EXPERT'], title: '我的任务' } },
      { path: 'expert/review', component: () => import('../views/expert/ReviewWorkbenchView.vue'), meta: { roles: ['REVIEW_EXPERT'], title: '评审工作台' } },
      { path: 'expert/declarations', component: () => import('../views/expert/ExpertDeclarationsView.vue'), meta: { roles: ['REVIEW_EXPERT'], title: '回避申报' } },
      { path: 'expert/history', component: () => import('../views/expert/ExpertHistoryView.vue'), meta: { roles: ['REVIEW_EXPERT'], title: '评审历史' } },
      // ---------- 供应商端（P6.5） ----------
      { path: 'supplier', component: () => import('../views/supplier/SupplierDashboard.vue'), meta: { roles: ['SUPPLIER'], title: '招标市场' } },
      { path: 'supplier/projects/:projectId', component: () => import('../views/supplier/SupplierProjectLotsView.vue'), meta: { roles: ['SUPPLIER'], title: '项目详情' } },
      { path: 'supplier/lots/:lotId', component: () => import('../views/supplier/SupplierLotDetailView.vue'), meta: { roles: ['SUPPLIER'], title: '标段详情' } },
      { path: 'supplier/lots/:lotId/upload', component: () => import('../views/supplier/SupplierBidUploadView.vue'), meta: { roles: ['SUPPLIER'], title: '标书上传' } },
      { path: 'supplier/bids', component: () => import('../views/supplier/SupplierBidsView.vue'), meta: { roles: ['SUPPLIER'], title: '投标结果' } },
      { path: 'supplier/bids/:bidId', component: () => import('../views/supplier/SupplierBidDetailView.vue'), meta: { roles: ['SUPPLIER'], title: '标书详情' } },
      { path: 'supplier/results/:bidId', component: () => import('../views/supplier/SupplierResultDetailView.vue'), meta: { roles: ['SUPPLIER'], title: '结果详情' } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/login' },
]

const router = createRouter({ history: createWebHistory(), routes })

// 全局守卫：登录校验 + 角色鉴权 + 根路径按角色落地
router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.public) return true
  if (!auth.isLoggedIn) return { path: '/login', query: { redirect: to.fullPath } }
  if (to.path === '/') return auth.homePath()
  if (to.meta.roles && !to.meta.roles.includes(auth.role)) return auth.homePath()
  return true
})

export default router
