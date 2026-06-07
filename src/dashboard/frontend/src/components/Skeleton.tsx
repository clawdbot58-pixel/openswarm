/**
 * Skeletal loading shell — content-shape aware, never a generic spinner.
 */

import { cn } from "../utils/cn";

interface SkeletonProps {
  className?: string;
}

export function Skeleton({ className }: SkeletonProps): JSX.Element {
  return <div className={cn("skeleton", className)} aria-hidden="true" />;
}

export function AgentCardSkeleton(): JSX.Element {
  return (
    <div className="surface p-4 space-y-3" data-testid="agent-card-skeleton">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Skeleton className="h-2 w-2 rounded-full" />
          <Skeleton className="h-3 w-20" />
        </div>
        <Skeleton className="h-3 w-12" />
      </div>
      <div className="space-y-2">
        <Skeleton className="h-3 w-3/4" />
        <Skeleton className="h-3 w-1/2" />
        <Skeleton className="h-3 w-2/3" />
      </div>
    </div>
  );
}

export function PanelHeaderSkeleton(): JSX.Element {
  return (
    <div className="flex items-center justify-between mb-3">
      <Skeleton className="h-3 w-24" />
      <Skeleton className="h-3 w-12" />
    </div>
  );
}
