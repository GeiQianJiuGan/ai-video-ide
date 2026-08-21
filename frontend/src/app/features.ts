/**
 * 功能注册表 —— 界面的唯一真源。
 *
 * Activity Bar、项目管理页、命令面板、功能页四处共用这一份定义。
 * 任何新功能只在这里登记一次，四处同时出现，绝不会「导航里有、页面里没有」。
 *
 * scope 把功能分成两类，这是入口不再杂乱的根据：
 *   app     —— 不需要打开工程就有意义（项目管理、素材库）；
 *   project —— 必须先打开一个工程（其余全部）。没打开工程时它们干脆不出现，
 *              而不是画成一排灰色的锁。
 *
 * 字段刻意写成「用户视角」：purpose 说的是能拿到什么结果，不是它是什么组件。
 */

import type { Component } from 'vue'
import {
  Boxes,
  Clapperboard,
  Film,
  FolderTree,
  Gauge,
  Images,
  Library,
  LayoutGrid,
  ListVideo,
  MapPinned,
  Network,
  ScrollText,
  Users,
  Video,
  Workflow,
} from '@lucide/vue'

export type Milestone = 'M0' | 'M1' | 'M2' | 'M3' | 'M4' | 'M5' | 'M6'
export type Requirement = 'backend' | 'comfyui' | 'ffmpeg' | 'llm'
export type Scope = 'app' | 'project'
export type GroupId = 'app' | 'asset' | 'story' | 'generate' | 'assemble'

export interface FeatureGroup {
  id: GroupId
  title: string
  /** 这一层回答导演的哪个问题。 */
  question: string
}

export const GROUPS: FeatureGroup[] = [
  { id: 'app', title: '工作台', question: '做哪一部片子、从哪里取素材' },
  { id: 'asset', title: '素材层', question: '谁出场、在哪里、用什么道具' },
  { id: 'story', title: '叙事层', question: '讲什么故事、怎么切成镜头' },
  { id: 'generate', title: '生成层', question: '怎么一幕一幕把片子做出来' },
  { id: 'assemble', title: '成片层', question: '怎么拼成一条完整的片子' },
]

export interface PanelSpec {
  title: string
  /** 这个面板里会出现什么，用于空状态说明。 */
  body: string
}

export interface ActionSpec {
  label: string
  hint: string
  primary?: boolean
}

export interface Feature {
  id: string
  /** 路由 name。scope 为 project 的挂在 /p/:pid 下，app 的挂在顶层。 */
  route: string
  title: string
  /** 一句话说清「用它能拿到什么」。 */
  purpose: string
  scope: Scope
  group: GroupId
  icon: Component
  milestone: Milestone
  /** 是否已真正可用。false 时功能页显示能力锁，绝不给假界面。 */
  ready: boolean
  /**
   * 高级 / 兼容路径。默认主路走两级场景系统（flow → scene），
   * 标了 advanced 的功能不进导航，只在设置页与命令面板里出现——
   * 它们没有被删掉，只是不再是推荐做法。
   */
  advanced?: boolean
  requires: Requirement[]
  /** 工作区骨架：左栏 / 主区 / 右栏检查器 / 底部。 */
  panels: { left?: PanelSpec; main: PanelSpec; right?: PanelSpec; bottom?: PanelSpec }
  actions: ActionSpec[]
  /** 做完这一步，你手上多了什么。 */
  outcome: string[]
}

/** 登记时不写 scope——它由下面两张表决定，避免登记在项目表里却标成 app 这种自相矛盾。 */
type FeatureDef = Omit<Feature, 'scope'>

/** 不需要打开工程就有意义的功能。 */
const APP_DEFS: FeatureDef[] = [
  {
    id: 'projects',
    route: 'projects',
    title: '项目管理',
    purpose: '新建或打开一个工程目录——所有创作都从这里开始',
    group: 'app',
    icon: FolderTree,
    milestone: 'M1',
    ready: true,
    requires: ['backend'],
    panels: {
      main: { title: '项目', body: '新建 / 打开工程目录，最近打开列表' },
    },
    actions: [
      { label: '新建项目', hint: '选一个空文件夹，落一份 project.db', primary: true },
      { label: '打开已有工程', hint: '选中含 project.aivs.json 的目录' },
    ],
    outcome: ['一个自包含的工程目录，可整体拷走换机继续'],
  },
  {
    id: 'library',
    route: 'library',
    title: '素材库',
    purpose: '跨项目复用的素材与角色 / 地点 / 道具预设，采用到当前项目里',
    group: 'app',
    icon: Library,
    milestone: 'M1',
    ready: true,
    requires: ['backend'],
    panels: {
      left: { title: '分类与标签', body: '素材 / 角色 / 地点 / 道具，按标签筛选' },
      main: { title: '素材网格', body: '库内文件的缩略图与尺寸、大小' },
      right: { title: '采用到项目', body: '复制进当前工程的 assets/，并记下出处；采用后互不影响' },
    },
    actions: [
      { label: '选择素材库目录', hint: '库是一个独立目录，位置由你决定', primary: true },
      { label: '采用到当前项目', hint: '复制一份副本进工程，工程保持自包含' },
    ],
    outcome: ['换一部片子不用从零重建角色与素材', '工程里拿到的是可再改的副本'],
  },
]

/** 必须先打开一个工程才有意义的功能。 */
const PROJECT_DEFS: FeatureDef[] = [
  {
    id: 'dashboard',
    route: 'dashboard',
    title: '项目概览',
    purpose: '一眼看到这个项目做到哪了：多少镜头已生成、总时长多久、卡在哪里',
    group: 'story',
    icon: Gauge,
    milestone: 'M1',
    ready: true,
    requires: ['backend'],
    panels: {
      main: { title: '进度总览', body: 'Scene / Shot 数量、已生成与待生成、按状态分布的柱状条' },
      right: { title: '最近活动', body: '最近 10 条生成任务、当前 GPU 与显存占用' },
    },
    actions: [
      { label: '继续上次工作', hint: '跳到最近编辑的 Shot', primary: true },
      { label: '连续性检查', hint: '遍历全部镜头找缺口；只报事实，不自动改数据' },
    ],
    outcome: ['知道下一步该做什么', '发现被上游阻塞的镜头'],
  },
  {
    id: 'characters',
    route: 'characters',
    title: '角色工作台',
    purpose: '把一个角色的长相固定下来，之后每个镜头都长得一样',
    group: 'asset',
    icon: Users,
    milestone: 'M1',
    ready: true,
    requires: ['backend', 'comfyui'],
    panels: {
      left: { title: '角色列表', body: '按项目列出所有角色，显示形象数量与是否有 Character Sheet' },
      main: {
        title: '形象与角色表',
        body: '一个角色可有多个形象（少年 / 成年 / 战损），每个形象生成多视角 Character Sheet 并框选可用区域',
      },
      right: { title: '形象属性', body: '脸型、发型、体型、服装、状态；从父形象继承并按需覆写' },
    },
    actions: [
      { label: '新建角色', hint: '只需名字即可创建，外观可以之后再补', primary: true },
      { label: '从素材库采用', hint: '复制一份库内预设进工程，之后两边各改各的' },
      { label: '派生形象', hint: '基于已有形象改年龄或服装，其余特征自动继承' },
      { label: '生成角色表', hint: '需要 ComfyUI 与生成队列；当前只支持上传角色表' },
    ],
    outcome: ['一张可复用的多视角角色参考图', '后续镜头自动引用它，人物不会走形'],
  },
  {
    id: 'locations',
    route: 'locations',
    title: '场景工作台',
    purpose: '把一个地点的样子固定下来，并管理它的日夜雨雪变体',
    group: 'asset',
    icon: MapPinned,
    milestone: 'M1',
    ready: true,
    requires: ['backend', 'comfyui'],
    panels: {
      left: { title: '地点列表', body: '所有地点，及每个地点下的变体数量' },
      main: {
        title: '变体与参考图',
        body: '同一地点的白天 / 夜晚 / 雨天变体，每个变体可出多机位参考图',
      },
      right: { title: '变体属性', body: '时间、天气、光线描述，以及被哪些 Scene 引用' },
    },
    actions: [
      { label: '新建地点', hint: '先建地点，再往下加变体', primary: true },
      { label: '从素材库采用', hint: '复制一份库内预设进工程，之后两边各改各的' },
      { label: '添加变体', hint: '同一处的白天与雨夜各是一个变体，Scene 引用的是变体' },
      { label: '生成参考图', hint: '需要 ComfyUI 与生成队列；当前只支持上传参考图' },
    ],
    outcome: ['同一地点在不同镜头里保持一致', '换天气不需要重建地点'],
  },
  {
    id: 'props',
    route: 'props',
    title: '道具库',
    purpose: '登记反复出现的关键道具，并知道它被哪些镜头用到',
    group: 'asset',
    icon: Boxes,
    milestone: 'M1',
    ready: true,
    requires: ['backend'],
    panels: {
      left: { title: '道具列表', body: '所有道具及其参考图版本数' },
      main: { title: '参考图', body: '同一道具的多个参考版本，只增不改：新版本自动成为当前' },
      right: { title: '引用位置', body: '反查：这个道具出现在多少个镜头里，删之前先看清影响' },
    },
    actions: [
      { label: '新建道具', hint: '名字 + 一句描述即可', primary: true },
      { label: '从素材库采用', hint: '复制一份库内预设进工程，之后两边各改各的' },
      { label: '上传参考图', hint: '新版本自动成为当前，旧版本永不覆盖' },
    ],
    outcome: ['关键道具在跨镜头时不会变样', '改道具前先看清影响范围'],
  },
  {
    id: 'story',
    route: 'story',
    title: '剧本与 AI 导演',
    purpose: '把一段文字剧本拆成 Scene 与 Shot，AI 可选、手动同样能走完',
    group: 'story',
    icon: ScrollText,
    milestone: 'M3',
    ready: true,
    requires: ['backend'],
    panels: {
      left: { title: '剧本', body: '分段文本编辑，段落与 Scene 双向对应' },
      main: {
        title: '拆解结果',
        body: 'AI 拆出的 Scene / Shot 逐项 Diff：你逐条接受或改写，确认后才落库',
      },
      right: { title: '角色映射', body: '文本里的人名映射到已有角色，避免重复建人' },
    },
    actions: [
      { label: 'AI 自动拆解', hint: '需要配置 LLM；产出结果仍需你审阅', primary: true },
      { label: '手动添加 Scene', hint: '不依赖 LLM，完整流程照样可用' },
      { label: '挂地点变体', hint: '把一场戏钉在「城南旧宅 · 雨夜」上，镜头顺着它取参考图' },
    ],
    outcome: ['一份结构化的 Scene / Shot 清单', '每个镜头都挂好了角色与地点'],
  },
  {
    id: 'storyboard',
    route: 'storyboard',
    title: '分镜板',
    purpose: '整片的鸟瞰视图：每个镜头一张卡片，缺什么、生成到哪一步都写在脸上',
    group: 'story',
    icon: LayoutGrid,
    milestone: 'M3',
    ready: true,
    requires: ['backend'],
    panels: {
      main: {
        title: 'Scene 泳道',
        body: '按 Scene 分行，Shot 卡片显示缩略图、时长、出场角色、状态点与上下文完备度',
      },
      right: { title: '批量操作', body: '选中多个镜头后统一生成、改时长、改 Workflow' },
    },
    actions: [
      { label: '生成整个 Scene', hint: '把这一场的所有镜头一次性入队', primary: true },
      { label: '移动镜头', hint: '本场内换位或跨场搬，顺序即时间顺序' },
      { label: '按状态筛选', hint: '只看失败的、只看缺上下文的' },
    ],
    outcome: ['一眼看出整片进度与缺口', '批量推进而不用一个个点'],
  },
  {
    id: 'shot',
    route: 'shot',
    title: '镜头编辑器',
    purpose: '单个镜头的全部真相：上下文喂了什么、用哪套 Workflow、生成了几个版本',
    group: 'generate',
    icon: Clapperboard,
    milestone: 'M4',
    ready: true,
    requires: ['backend', 'comfyui'],
    panels: {
      left: {
        title: '镜头信息',
        body: '时长、机位、运镜、出场角色与其状态（入场 / 已在场 / 背景）',
      },
      main: {
        title: '上下文检查器',
        body: '逐条列出真正喂给模型的参考：来源、优先级、为什么被包含或被省略，可手动移除或替换',
      },
      right: { title: '版本轨', body: '历次生成的版本，可预览、A-B 对比、设为当前、加入时间线' },
      bottom: {
        title: 'Prompt 与参数',
        body: '正负向 Prompt、种子、步数；每个版本都冻结当时的取值',
      },
    },
    actions: [
      {
        label: '生成',
        hint: '按当前上下文与 Workflow 生成一个新版本，不覆盖旧版本',
        primary: true,
      },
      { label: '生成 N 个候选', hint: '一次出多个，挑一个设为当前' },
      { label: '改用别的 Workflow', hint: '业务不绑定具体模型，换 Workflow 不改数据' },
    ],
    outcome: ['一个可用的镜头视频版本', '完整可追溯：这条片段是怎么来的'],
  },
  {
    id: 'flow',
    route: 'flow',
    title: '幕流程图',
    purpose: '整片就这一张图：有哪几幕、每一幕做到哪了、两幕之间怎么接',
    group: 'generate',
    icon: Network,
    milestone: 'M4',
    ready: true,
    requires: ['backend'],
    panels: {
      main: {
        title: '幕与衔接',
        body: '一个节点是一幕（缩略图、出场角色、进度徽标），节点之间那一条是衔接：硬切 / 转场 / 续接末帧',
      },
      right: {
        title: '编排账单',
        body: '这次会入队几个任务、要补几段转场、哪一条缺什么；执行完把「做了的」和「说好的」摆在一起',
      },
    },
    actions: [
      { label: '先看账单', hint: '只算不做：把要生成的、要补的、缺的全列出来', primary: true },
      { label: '执行编排', hint: '各幕并发（补转场）或单线程续接（上一段末帧当下一段首帧）' },
      { label: '加一幕', hint: '不依赖 LLM，手动编排能走完全程' },
    ],
    outcome: ['整片结构一眼看全', '两幕之间怎么接是显式选择，不是默认行为'],
  },
  {
    id: 'scene',
    route: 'scene',
    title: '幕工作台',
    purpose: '一幕的小工作坊：给镜头首帧（角色表 / 地点参考 / 上传 / 上游末帧），生成并留版本',
    group: 'generate',
    icon: Video,
    milestone: 'M4',
    ready: true,
    requires: ['backend'],
    panels: {
      left: { title: '这一幕', body: '地点变体、时间、镜头清单与本镜头出场角色' },
      main: { title: '首帧与上下文', body: '首帧的四条来路，以及真正喂给模型的那张账单' },
      right: { title: '版本轨', body: '历次生成只增不改；也能手动导入一个成片' },
      bottom: { title: 'Prompt 与参数', body: '正负向 Prompt、时长、种子；入队那一刻被冻结进版本' },
    },
    actions: [
      { label: '生成本镜头', hint: '按当前首帧与上下文入队一个新版本', primary: true },
      { label: '整幕入队', hint: '这一幕里可生成的一次入队，缺上下文的写明原因后跳过' },
      { label: '接上游末帧', hint: '生成前抽上游的真末帧当首帧，等待可解释' },
    ],
    outcome: ['一幕的每个镜头都有可用的视频版本', '首帧是哪来的写在脸上'],
  },
  {
    id: 'workflows',
    route: 'workflows',
    title: 'Workflow 管理（高级）',
    purpose: '高级 / 兼容路径：手工把 ComfyUI 工作流的节点字段绑成能力，只有主路满足不了时才用',
    group: 'generate',
    icon: Workflow,
    milestone: 'M2',
    ready: true,
    advanced: true,
    requires: ['backend', 'comfyui'],
    panels: {
      left: {
        title: 'Workflow 列表',
        body: '已导入的工作流，按能力分类（文生图 / 图生视频 / 首尾帧）',
      },
      main: {
        title: '节点绑定',
        body: '导入 workflow_api.json 后，把 prompt、参考图、时长、种子等输入绑定到具体节点字段',
      },
      right: {
        title: '能力矩阵',
        body: '哪些能力已有 Workflow、哪些还缺，缺的会导致对应镜头无法生成',
      },
    },
    actions: [
      { label: '导入 Workflow', hint: '从 ComfyUI 导出 API 格式的 json 后拖进来', primary: true },
      { label: '校验绑定', hint: '检查节点是否存在、自定义节点是否已安装' },
      { label: '设为默认', hint: '新镜头默认使用这套 Workflow' },
    ],
    outcome: [
      '换模型只需换 Workflow，镜头数据完全不动',
      '主路（幕流程图 → 幕工作台）不需要它：模型端的图由模型端维护',
    ],
  },
  {
    id: 'queue',
    route: 'queue',
    title: '生成队列（细看）',
    purpose:
      '队列的常驻界面是底部控制台的任务框；这一页是点开细看的那层：失败现场、入队时冻结的参数、优先级',
    group: 'generate',
    icon: ListVideo,
    milestone: 'M4',
    ready: true,
    advanced: true,
    requires: ['backend', 'comfyui'],
    panels: {
      main: {
        title: '任务列表',
        body: '按 DAG 依赖排队，实时进度百分比；显示等待的上游是哪个任务',
      },
      right: { title: '失败现场', body: '结构化错误码、修复建议、ComfyUI 原始报错与节点图快照' },
    },
    actions: [
      { label: '暂停队列', hint: '正在跑的任务不中断，只停止取新任务', primary: true },
      { label: '重试失败任务', hint: '沿用原参数重跑，仍然不覆盖任何旧版本' },
      { label: '调整优先级', hint: '把某个镜头提到队首' },
    ],
    outcome: [
      '长时间批量生成可以放手不看',
      '失败有明确的下一步动作，绝不静默失败',
      '日常盯任务不用离开当前页面：状态条上点一下就升起控制台',
    ],
  },
  {
    id: 'timeline',
    route: 'timeline',
    title: '时间线',
    purpose: '把选定的镜头版本拼成成片并导出，不依赖 AI 也能完整工作',
    group: 'assemble',
    icon: Film,
    milestone: 'M5',
    ready: true,
    requires: ['backend', 'ffmpeg'],
    panels: {
      main: {
        title: '轨道区',
        body: 'Canvas 渲染的多轨时间线：视频轨缩略图条、音频轨波形、缩放与吸附',
      },
      right: { title: '片段属性', body: '入出点、速度、转场、字幕；改动进入撤销栈' },
      bottom: { title: '预览', body: '720p 代理流预览，拖动即定位，导出时才用原始素材' },
    },
    actions: [
      {
        label: '自动装配',
        hint: '按 Scene / Shot 顺序把每个镜头的当前版本铺到轨道上',
        primary: true,
      },
      { label: '替换为其他版本', hint: '选中片段后换成同一镜头的另一个生成版本' },
      { label: '导出成片', hint: '走 FFmpeg filter_complex，用原始素材而非代理' },
    ],
    outcome: ['一条可导出的完整视频', '换掉某个镜头不需要重排整条时间线'],
  },
  {
    id: 'assets',
    route: 'assets',
    title: '资产库',
    purpose: '所有落盘文件的总账：哪来的、被谁用了、哪些是没人要的孤儿',
    group: 'assemble',
    icon: Images,
    milestone: 'M1',
    ready: true,
    requires: ['backend'],
    panels: {
      left: { title: '按类型筛选', body: '角色表 / 场景参考 / 道具图 / 生成视频 / 抽帧 / 代理' },
      main: { title: '资产网格', body: '缩略图、分辨率、大小、创建时间' },
      right: { title: '引用关系', body: '这个文件被哪些角色、镜头、版本引用；孤儿资产可安全清理' },
    },
    actions: [
      { label: '扫描孤儿资产', hint: '找出没有任何引用的文件，回收磁盘', primary: true },
      { label: '从素材库采用', hint: '把库里的文件复制一份进工程，先出账单再动手' },
      { label: '定位引用来源', hint: '从文件跳到使用它的镜头' },
    ],
    outcome: ['磁盘占用可控', '删文件之前先知道会破坏什么'],
  },
]

export const FEATURES: Feature[] = [
  ...APP_DEFS.map((f) => ({ ...f, scope: 'app' as const })),
  ...PROJECT_DEFS.map((f) => ({ ...f, scope: 'project' as const })),
]

/** 核心链路节点。项目概览页把它画成一条可点击的流水线，让「系统怎么工作」自解释。 */
export interface ChainNode {
  label: string
  /** 点进去落到哪个功能；null 表示这是系统内部产物，没有独立页面。 */
  route: string | null
  desc: string
}

export const CHAIN: ChainNode[] = [
  { label: 'Character', route: 'characters', desc: '角色本体' },
  { label: 'Appearance', route: 'characters', desc: '同一角色的不同形象' },
  { label: 'Scene', route: 'flow', desc: '一场戏：地点 + 时间 + 出场角色' },
  { label: 'Shot', route: 'storyboard', desc: '一个镜头' },
  // Context / Generation 现在的主路是幕工作台，但它必须带着 sid 才有意义，
  // 所以链路上指向流程图——从那儿点一个节点进去。
  { label: 'Context', route: 'flow', desc: '真正喂给模型的参考集合' },
  { label: 'Generation', route: 'flow', desc: '一次生成请求' },
  { label: 'Version', route: 'shot', desc: '生成结果，永不覆盖' },
  { label: 'Clip', route: 'timeline', desc: '被选中进入成片的片段' },
  { label: 'Timeline', route: 'timeline', desc: '确定性的编辑系统' },
  { label: 'Final Video', route: 'timeline', desc: '导出成片' },
]

export const REQUIREMENT_LABEL: Record<Requirement, string> = {
  backend: '后端服务',
  comfyui: 'ComfyUI',
  ffmpeg: 'FFmpeg',
  llm: 'LLM（可选）',
}

export function featureByRoute(route: string | null | undefined): Feature | null {
  if (!route) return null
  return FEATURES.find((f) => f.route === route) ?? null
}

export function featuresOf(group: GroupId): Feature[] {
  return FEATURES.filter((f) => f.group === group)
}

/**
 * 应用级导航：没打开工程时左栏只有这些。
 *
 * 打开工程后它们**消失**——两级互斥。混在同一条栏里的时候，「项目」看着像个页面，
 * 点下去却是退出当前工程，那是个随时会踩到的地雷。现在退出只有一个显式入口：
 * 左栏顶上的「← 项目列表」。工程内要取库里的东西走各页的「从素材库采用」
 * （角色 / 地点 / 道具在各自的页面，素材文件在资产库页），不必掉出工程再回来。
 */
export const APP_NAV = ['projects', 'library'] as const

/**
 * 项目内导航顺序：按创作流程走，而不是按字母。
 *
 * 生成层的入口是 `flow`（幕流程图）——从整片结构进到某一幕，再进那一幕的工作台。
 * `scene` 不在这里：它只从流程图点节点进去（URL 必须带 sid）。
 * `workflows` 也不在这里：它是 `advanced: true` 的兼容路径，从命令面板或设置页进。
 * `queue` 同样不在这里：队列是**一直在跑的东西**而不是一个要走过去看的页面，
 * 它的常驻界面是底部控制台的任务框（状态条上那个任务标识点一下就升起来）；
 * 队列页只留给「点开细看」——失败现场、冻结参数、优先级细调。
 */
export const PROJECT_NAV = [
  'dashboard',
  'characters',
  'locations',
  'props',
  'story',
  'storyboard',
  'flow',
  'timeline',
  'assets',
] as const

function pick(ids: readonly string[]): Feature[] {
  return ids
    .map((id) => FEATURES.find((f) => f.id === id))
    .filter((f): f is Feature => f !== undefined)
}

export const APP_NAV_FEATURES: Feature[] = pick(APP_NAV)
export const PROJECT_NAV_FEATURES: Feature[] = pick(PROJECT_NAV)
/**
 * 高级 / 兼容路径的项目内功能：不进左栏导航，但命令面板里要能搜到。
 * 「不推荐」不等于「藏起来找不到」——已经配好的东西必须还能进去。
 */
export const PROJECT_ADVANCED_FEATURES: Feature[] = FEATURES.filter(
  (f) => f.scope === 'project' && f.advanced === true,
)

/** Activity Bar 只有 48px 宽，需要更短的标签；tooltip 里给全名与作用。 */
export const NAV_LABEL: Record<string, string> = {
  projects: '项目',
  library: '素材库',
  dashboard: '概览',
  characters: '角色',
  locations: '场景',
  props: '道具',
  story: '剧本',
  storyboard: '分镜',
  flow: '幕',
  scene: '幕内',
  workflows: '流程',
  queue: '队列细看',
  timeline: '时间线',
  assets: '资产',
}
