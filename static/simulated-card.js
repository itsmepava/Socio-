(() => {
  const form = document.getElementById('simulated-card-form');
  const error = document.getElementById('demo-card-error');
  if (!form || !error) return;

  form.addEventListener('submit', (event) => {
    const digits = document.getElementById('demo-card-number').value.replace(/\D/g, '');
    let sum = 0;
    let doubleDigit = false;
    for (let index = digits.length - 1; index >= 0; index -= 1) {
      let value = Number(digits[index]);
      if (doubleDigit) { value *= 2; if (value > 9) value -= 9; }
      sum += value;
      doubleDigit = !doubleDigit;
    }

    const expiry = document.getElementById('demo-card-expiry').value.trim();
    const expiryMatch = /^(0[1-9]|1[0-2])\/(\d{2})$/.exec(expiry);
    let expiryValid = false;
    if (expiryMatch) {
      const month = Number(expiryMatch[1]);
      const year = 2000 + Number(expiryMatch[2]);
      const today = new Date();
      expiryValid = year > today.getFullYear() ||
        (year === today.getFullYear() && month >= today.getMonth() + 1);
    }
    const cvc = document.getElementById('demo-card-cvc').value.trim();
    if (!/^\d{12,19}$/.test(digits) || sum % 10 !== 0 ||
        !expiryValid || !/^\d{3,4}$/.test(cvc)) {
      event.preventDefault();
      error.textContent = 'Enter a valid demo card number, a future expiry, and a 3 or 4 digit CVC.';
      return;
    }
    error.textContent = '';
  });
})();
