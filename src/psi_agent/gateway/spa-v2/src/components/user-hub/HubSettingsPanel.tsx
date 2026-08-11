import { ChevronRight, FolderOpen, Package } from 'lucide-react'
import HubDialog from './HubDialog'

type Props = {
  show: boolean
  onClose: () => void
  workspace?: string
  onChangeWorkspace?: () => void
  agent?: string
  onChangeAgent?: () => void
}

function pathLabel(path: string): string {
  const p = path.replace(/\\/g, '/').replace(/\/+$/, '')
  const parts = p.split('/').filter(Boolean)
  return parts[parts.length - 1] || p || '未选择'
}

/** Settings dialog — workspace + agent package path switches. */
export default function HubSettingsPanel({
  show,
  onClose,
  workspace,
  onChangeWorkspace,
  agent,
  onChangeAgent,
}: Props) {
  return (
    <HubDialog
      show={show}
      title="设置"
      width={420}
      onClose={onClose}
      actions={<button type="button" className="hub-btn primary" onClick={onClose}>关闭</button>}
    >
      <section className="hub-settings-section">
        <h4>工作区与 Agent 包</h4>
        {onChangeWorkspace ? (
          <button
            type="button"
            className="hub-settings-row hub-settings-workspace"
            onClick={() => {
              onClose()
              onChangeWorkspace()
            }}
          >
            <span className="hub-settings-workspace-icon" aria-hidden="true">
              <FolderOpen size={18} />
            </span>
            <span>
              <strong>切换工作区</strong>
              <em title={workspace || undefined}>
                {workspace ? pathLabel(workspace) : '选择本机目录'}
              </em>
            </span>
            <ChevronRight size={16} className="hub-settings-row-chevron" />
          </button>
        ) : (
          <p className="hub-settings-workspace-path">{workspace || '未选择工作区'}</p>
        )}
        {workspace && onChangeWorkspace ? (
          <p className="hub-settings-workspace-path" title={workspace}>{workspace}</p>
        ) : null}

        {onChangeAgent ? (
          <button
            type="button"
            className="hub-settings-row hub-settings-workspace"
            onClick={() => {
              onClose()
              onChangeAgent()
            }}
          >
            <span className="hub-settings-workspace-icon" aria-hidden="true">
              <Package size={18} />
            </span>
            <span>
              <strong>切换 Agent 包</strong>
              <em title={agent || undefined}>
                {agent ? pathLabel(agent) : '选择能力包目录'}
              </em>
            </span>
            <ChevronRight size={16} className="hub-settings-row-chevron" />
          </button>
        ) : null}
        {agent && onChangeAgent ? (
          <p className="hub-settings-workspace-path" title={agent}>{agent}</p>
        ) : null}
        {onChangeAgent ? (
          <p className="hub-settings-foot">
            Agent 包含 tools / schedules / systems。切换后仅影响<strong>新建任务/聊天</strong>；
            已有任务仍使用创建时绑定的包。
          </p>
        ) : null}
      </section>
    </HubDialog>
  )
}
