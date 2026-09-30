<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::table('runtime_contexts', function (Blueprint $table) {
            $table->foreignId('executor_private_key_id')->nullable()->constrained('private_keys')->restrictOnDelete();
            $table->text('ssh_host_key')->nullable();
            $table->unsignedBigInteger('controller_epoch')->nullable();
            $table->unsignedBigInteger('policy_version')->nullable();
        });
    }

    public function down(): void
    {
        Schema::table('runtime_contexts', function (Blueprint $table) {
            $table->dropConstrainedForeignId('executor_private_key_id');
            $table->dropColumn(['ssh_host_key', 'controller_epoch', 'policy_version']);
        });
    }
};
