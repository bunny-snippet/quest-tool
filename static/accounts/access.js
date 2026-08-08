(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const csrf = $('.csrf-source input')?.value || '';
  const backdrop = $('[data-modal-backdrop]');
  const userModal = $('#userModal');
  const roleModal = $('#roleModal');
  const confirmModal = $('#confirmModal');
  const userForm = $('#userForm');
  const roleForm = $('#roleForm');
  let userId = null;
  let roleSlug = null;
  let deleteTarget = null;

  function toast(message, error = false) {
    const node = $('[data-access-toast]');
    node.textContent = message;
    node.classList.toggle('error', error);
    node.classList.add('show');
    setTimeout(() => node.classList.remove('show'), 3200);
  }

  function showModal(modal) {
    backdrop.hidden = false;
    modal.hidden = false;
    requestAnimationFrame(() => { backdrop.classList.add('open'); modal.classList.add('open'); });
    document.body.classList.add('modal-open');
    setTimeout(() => $('input:not([type=hidden]),select', modal)?.focus(), 120);
  }

  function closeModals() {
    backdrop.classList.remove('open');
    $$('.access-modal.open,.confirm-modal.open').forEach((node) => node.classList.remove('open'));
    document.body.classList.remove('modal-open');
    setTimeout(() => { backdrop.hidden = true; [userModal, roleModal, confirmModal].forEach((node) => { node.hidden = true; }); }, 180);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, { credentials: 'same-origin', ...options, headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, ...(options.headers || {}) } });
    const data = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = Object.entries(data || {}).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(', ') : value}`).join(' · ') || 'Request could not be completed.';
      throw new Error(message);
    }
    return data;
  }

  function resetUserForm() {
    userForm.reset();
    userForm.elements.is_active.checked = true;
    userId = null;
    $('#userModalTitle').textContent = 'Add user';
    $('[data-user-submit]').textContent = 'Create user';
    $('[data-password-note]').textContent = 'Required for a new user, minimum 8 characters.';
    $$('[data-function] select', userModal).forEach((select) => { select.value = 'role'; });
    $('[data-user-error]').hidden = true;
  }

  $$('[data-open-user]').forEach((button) => button.addEventListener('click', () => { resetUserForm(); showModal(userModal); }));
  if (new URLSearchParams(window.location.search).get('open') === 'user' && userModal) {
    resetUserForm();
    if (userForm.elements.account_type) userForm.elements.account_type.value = 'internal_vendor';
    showModal(userModal);
    history.replaceState({}, '', window.location.pathname);
  }
  $$('[data-edit-user]').forEach((button) => button.addEventListener('click', async () => {
    resetUserForm(); userId = button.dataset.editUser;
    try {
      const data = await api(`/api/v1/access/users/${userId}/`);
      userForm.elements.first_name.value = data.first_name || '';
      userForm.elements.last_name.value = data.last_name || '';
      userForm.elements.email.value = data.email || '';
      userForm.elements.account_type.value = data.account_type_details?.value || 'employee';
      userForm.elements.company_name.value = data.company || '';
      userForm.elements.department.value = data.sub_branch || '';
      userForm.elements.role.value = data.role_details?.slug || '';
      userForm.elements.is_active.checked = data.is_active;
      (data.allowed_overrides || []).forEach((code) => { const item = $(`[data-function="${CSS.escape(code)}"] select`, userModal); if (item) item.value = 'allow'; });
      (data.denied_overrides || []).forEach((code) => { const item = $(`[data-function="${CSS.escape(code)}"] select`, userModal); if (item) item.value = 'deny'; });
      $('#userModalTitle').textContent = 'Edit user access'; $('[data-user-submit]').textContent = 'Save changes';
      $('[data-password-note]').textContent = 'Leave blank to keep the current password.';
      showModal(userModal);
    } catch (error) { toast(error.message, true); }
  }));

  userForm?.addEventListener('submit', async (event) => {
    event.preventDefault(); const errorBox = $('[data-user-error]'); errorBox.hidden = true;
    const allow = [], deny = [];
    $$('[data-function]', userModal).forEach((row) => { const value = $('select', row).value; if (value === 'allow') allow.push(row.dataset.function); if (value === 'deny') deny.push(row.dataset.function); });
    const payload = { first_name: userForm.elements.first_name.value.trim(), last_name: userForm.elements.last_name.value.trim(), email: userForm.elements.email.value.trim(), role: userForm.elements.role.value, account_type: userForm.elements.account_type.value, company_name: userForm.elements.company_name.value.trim(), department: userForm.elements.department.value.trim(), is_active: userForm.elements.is_active.checked, allow_codes: allow, deny_codes: deny };
    if (userForm.elements.password.value) payload.password = userForm.elements.password.value;
    try {
      await api(userId ? `/api/v1/access/users/${userId}/` : '/api/v1/access/users/', { method: userId ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
      toast(userId ? 'User access updated.' : 'User created successfully.'); closeModals(); setTimeout(() => location.reload(), 450);
    } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
  });

  function resetRoleForm() { roleForm.reset(); roleForm.elements.rank.value = 10; roleForm.elements.is_active.checked = true; roleSlug = null; $('#roleModalTitle').textContent = 'Create role'; $('[data-role-submit]').textContent = 'Create role'; $('[data-role-error]').hidden = true; }
  $('[data-open-role]')?.addEventListener('click', () => { resetRoleForm(); showModal(roleModal); });
  roleForm?.elements.name.addEventListener('input', () => { if (!roleSlug) roleForm.elements.slug.value = roleForm.elements.name.value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); });
  $$('[data-edit-role]').forEach((button) => button.addEventListener('click', async () => {
    resetRoleForm(); roleSlug = button.dataset.editRole;
    try {
      const data = await api(`/api/v1/access/roles/${encodeURIComponent(roleSlug)}/`);
      roleForm.elements.name.value = data.name; roleForm.elements.slug.value = data.slug; roleForm.elements.rank.value = data.rank; roleForm.elements.description.value = data.description || ''; roleForm.elements.is_active.checked = data.is_active;
      (data.effective_permission_codes || []).forEach((code) => { const input = $(`input[name="permission_codes"][value="${CSS.escape(code)}"]`, roleModal); if (input) input.checked = true; });
      $('#roleModalTitle').textContent = 'Edit role'; $('[data-role-submit]').textContent = 'Save role'; showModal(roleModal);
    } catch (error) { toast(error.message, true); }
  }));
  roleForm?.addEventListener('submit', async (event) => {
    event.preventDefault(); const errorBox = $('[data-role-error]'); errorBox.hidden = true;
    const payload = { name: roleForm.elements.name.value.trim(), slug: roleForm.elements.slug.value.trim(), rank: Number(roleForm.elements.rank.value), description: roleForm.elements.description.value.trim(), is_active: roleForm.elements.is_active.checked, permission_codes: $$('input[name="permission_codes"]:checked', roleModal).map((input) => input.value) };
    try {
      await api(roleSlug ? `/api/v1/access/roles/${encodeURIComponent(roleSlug)}/` : '/api/v1/access/roles/', { method: roleSlug ? 'PATCH' : 'POST', body: JSON.stringify(payload) });
      toast(roleSlug ? 'Role updated.' : 'Role created.'); closeModals(); setTimeout(() => location.reload(), 450);
    } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
  });

  function requestDelete(type, id, label) { deleteTarget = { type, id }; $('[data-delete-type]').textContent = type; $('[data-delete-label]').textContent = label; showModal(confirmModal); }
  $$('[data-delete-user]').forEach((button) => button.addEventListener('click', () => requestDelete('user', button.dataset.deleteUser, button.dataset.label)));
  $$('[data-delete-role]').forEach((button) => button.addEventListener('click', () => requestDelete('role', button.dataset.deleteRole, button.dataset.label)));
  $('[data-confirm-delete]')?.addEventListener('click', async () => { if (!deleteTarget) return; const url = deleteTarget.type === 'user' ? `/api/v1/access/users/${deleteTarget.id}/` : `/api/v1/access/roles/${encodeURIComponent(deleteTarget.id)}/`; try { await api(url, { method: 'DELETE' }); toast(`${deleteTarget.type} deleted.`); closeModals(); setTimeout(() => location.reload(), 450); } catch (error) { closeModals(); toast(error.message, true); } });
  $$('[data-close-modal]').forEach((button) => button.addEventListener('click', closeModals));
  backdrop?.addEventListener('click', closeModals);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeModals(); });
})();
