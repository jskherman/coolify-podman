<?php

namespace App\Console\Commands;

use App\Jobs\ExecuteNodeOperationJob;
use App\Models\NodeOperation;
use Illuminate\Console\Command;
use Illuminate\Database\Eloquent\Builder;

class ReconcileNodeOperations extends Command
{
    protected $signature = 'node:reconcile';

    protected $description = 'Redeliver unresolved node requests with their original operation IDs';

    public function handle(): int
    {
        $operations = NodeOperation::query()->whereIn('status', ['queued', 'executing', 'uncertain'])
            ->where(fn (Builder $query) => $query->where('attempts', '<', 5)
                ->orWhere(fn (Builder $query) => $query->where('action', 'activate')->where('status', 'executing')->where('result->status', 'executing')))
            ->where(fn (Builder $query) => $query->whereNull('lease_expires_at')->orWhere('lease_expires_at', '<=', now()))
            ->oldest('updated_at')->limit(100)->get();
        foreach ($operations as $operation) {
            ExecuteNodeOperationJob::dispatch($operation->id, $operation->actor_id)->afterCommit();
        }
        $this->info('Queued '.$operations->count().' recorded node operations for reconciliation.');

        return self::SUCCESS;
    }
}
