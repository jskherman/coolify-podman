<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('runtime_contexts', function (Blueprint $table) {
            $table->id();
            $table->string('uuid')->unique();
            $table->foreignId('server_id')->constrained()->cascadeOnDelete();
            $table->string('runtime');
            $table->unsignedBigInteger('execution_uid');
            $table->unsignedBigInteger('execution_gid');
            $table->string('manager');
            $table->string('identity_hash', 64)->unique();
            $table->string('connection_fingerprint', 64)->index();
            $table->string('capability_fingerprint', 64);
            $table->json('storage');
            $table->json('versions');
            $table->json('capabilities');
            $table->timestamp('probed_at');
            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('runtime_contexts');
    }
};
