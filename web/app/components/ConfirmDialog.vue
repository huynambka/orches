<script setup lang="ts">
export interface ConfirmOptions {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

const dialog = ref<HTMLDialogElement | null>(null);
const title = ref('Confirm');
const message = ref('');
const confirmLabel = ref('Confirm');
const cancelLabel = ref('Cancel');
const danger = ref(false);

let resolver: ((value: boolean) => void) | null = null;

const settle = (value: boolean) => {
  const resolve = resolver;
  resolver = null;
  if (dialog.value?.open) dialog.value.close();
  resolve?.(value);
};

/** Open the dialog and resolve true on confirm, false on cancel/Escape. */
const ask = (options: ConfirmOptions): Promise<boolean> => {
  resolver?.(false); // a second request supersedes a pending one
  title.value = options.title ?? 'Confirm';
  message.value = options.message;
  confirmLabel.value = options.confirmLabel ?? 'Confirm';
  cancelLabel.value = options.cancelLabel ?? 'Cancel';
  danger.value = options.danger ?? false;
  dialog.value?.showModal();
  return new Promise((resolve) => { resolver = resolve; });
};

defineExpose({ ask });

// Escape or programmatic close without a button press counts as cancel.
const onClose = () => {
  const resolve = resolver;
  resolver = null;
  resolve?.(false);
};
onUnmounted(onClose);
</script>

<template>
  <dialog ref="dialog" class="modal confirm-modal" :aria-label="title" @close="onClose">
    <div class="modal-head">
      <h2 class="page-heading">{{ title }}</h2>
    </div>
    <div class="modal-body">
      <p class="confirm-message">{{ message }}</p>
      <div class="modal-actions">
        <button class="control" type="button" @click="settle(false)">{{ cancelLabel }}</button>
        <button
          :class="`control ${danger ? 'control-danger' : 'control-primary'}`"
          type="button"
          autofocus
          @click="settle(true)"
        >{{ confirmLabel }}</button>
      </div>
    </div>
  </dialog>
</template>
