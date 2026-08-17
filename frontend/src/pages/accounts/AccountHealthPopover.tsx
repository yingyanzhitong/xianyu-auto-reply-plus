import { AlertCircle, CheckCircle, HelpCircle, X, XCircle } from 'lucide-react'
import type { CSSProperties } from 'react'
import type { AccountHealth, AccountHealthItem } from '@/types'

interface AccountHealthPopoverProps {
  accountId: string
  health?: AccountHealth
  position: Pick<CSSProperties, 'top' | 'left'>
  onClose: () => void
}

const STATUS_STYLES: Record<AccountHealthItem['status'], { icon: typeof CheckCircle; color: string }> = {
  ready: { icon: CheckCircle, color: 'text-emerald-500' },
  warning: { icon: AlertCircle, color: 'text-amber-500' },
  blocked: { icon: XCircle, color: 'text-rose-500' },
  disabled: { icon: HelpCircle, color: 'text-slate-400' },
  unknown: { icon: HelpCircle, color: 'text-sky-500' },
  offline: { icon: XCircle, color: 'text-slate-400' },
  degraded: { icon: AlertCircle, color: 'text-amber-500' },
}

const FALLBACK_HEALTH: AccountHealth = {
  overall: { status: 'unknown', label: '状态待确认', detail: '暂未获取账号健康度数据' },
  connection: { status: 'unknown', label: '连接', detail: '暂未获取连接状态' },
  token: { status: 'unknown', label: 'Token', detail: '暂未获取 Token 状态' },
  risk_control: { status: 'unknown', label: '风控', detail: '暂未获取风控状态' },
}

export function AccountHealthPopover({ accountId, health, position, onClose }: AccountHealthPopoverProps) {
  const resolvedHealth = health || FALLBACK_HEALTH
  const overallStyle = STATUS_STYLES[resolvedHealth.overall.status]
  const OverallIcon = overallStyle.icon
  const items = [
    resolvedHealth.connection,
    resolvedHealth.token,
    resolvedHealth.risk_control,
  ]

  return (
    <div
      role="dialog"
      aria-label={accountId + '运行状态'}
      className="fixed z-50 max-h-[calc(100vh-16px)] w-[320px] overflow-y-auto rounded-xl border border-slate-200 bg-white shadow-2xl shadow-slate-950/20 dark:border-slate-700 dark:bg-slate-900"
      style={position}
    >
      <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-4 py-3 dark:border-slate-800">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-sm font-semibold text-slate-900 dark:text-slate-100">
            <OverallIcon className={'h-4 w-4 shrink-0 ' + overallStyle.color} />
            <span>{resolvedHealth.overall.label}</span>
          </div>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{resolvedHealth.overall.detail}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭运行状态"
          className="rounded p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:hover:bg-slate-800 dark:hover:text-slate-200"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="divide-y divide-slate-100 px-4 dark:divide-slate-800">
        {items.map((item) => {
          const style = STATUS_STYLES[item.status]
          const Icon = style.icon
          return (
            <div key={item.label} className="flex items-start gap-2.5 py-2.5">
              <Icon className={'mt-0.5 h-4 w-4 shrink-0 ' + style.color} />
              <div className="min-w-0">
                <p className="text-xs font-medium text-slate-700 dark:text-slate-200">{item.label}</p>
                <p className="mt-0.5 text-xs leading-5 text-slate-500 dark:text-slate-400">{item.detail}</p>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
