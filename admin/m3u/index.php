<?php
// НАСТРОЙКИ БЕЗОПАСНОСТИ
$password = 'ЗАМЕНИТЕ_НА_СВОЙ_ПАРОЛЬ'; // Установите надежный пароль

session_start();

// Авторизация
if (isset($_POST['password'])) {
    if ($_POST['password'] === $password) {
        $_SESSION['logged_in'] = true;
    } else {
        $error = "Неверный пароль!";
    }
}

if (isset($_GET['logout'])) {
    session_destroy();
    header("Location: " . basename(__FILE__));
    exit;
}

// Список доступных для редактирования файлов
$allowed_files = [
    'tv'  => 'tv34online/tv34online/tv/playlist.m3u',
    'ios' => 'tv34online/tv34online/ios/playlist.m3u',
    'pc'  => 'tv34online/tv34online/pc/playlist.m3u'
];

// Определяем выбранный файл (по умолчанию первый)
$file_key = isset($_GET['file']) && array_key_exists($_GET['file'], $allowed_files) ? $_GET['file'] : 'tv';
$target_file = $allowed_files[$file_key];

$message = '';
if (isset($_SESSION['logged_in']) && $_SESSION['logged_in'] === true) {
    // Сохранение файла
    if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['content'])) {
        // Проверяем, существует ли директория, если нет — пробуем создать
        $dir = dirname($target_file);
        if (!is_dir($dir)) {
            mkdir($dir, 0777, true);
        }
        
        if (file_put_contents($target_file, $_POST['content']) !== false) {
            $message = "Плейлист успешно сохранен!";
        } else {
            $message = "Ошибка при сохранении файла! Проверьте права доступа (chmod).";
        }
    }
    
    $content = file_exists($target_file) ? file_get_contents($target_file) : '';
}
?>
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Управление плейлистами TV34Online</title>
    <style>
        body { font-family: sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 900px; margin: 0 auto; background: #1e293b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        textarea { width: 100%; height: 450px; font-family: monospace; font-size: 13px; background: #0f172a; color: #38bdf8; padding: 12px; border: 1px solid #334155; border-radius: 6px; box-sizing: border-box; resize: vertical; }
        button { background: #3b82f6; color: white; border: none; padding: 10px 20px; font-size: 15px; border-radius: 6px; cursor: pointer; margin-top: 10px; font-weight: bold; }
        button:hover { background: #2563eb; }
        .message { color: #4ade80; margin-bottom: 10px; font-weight: bold; }
        .error { color: #f87171; margin-bottom: 10px; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 1px solid #334155; padding-bottom: 10px; }
        select { background: #0f172a; color: #f8fafc; border: 1px solid #334155; padding: 8px; border-radius: 6px; font-size: 14px; }
        .login-box { max-width: 400px; margin: 100px auto; text-align: center; }
        .login-box input { padding: 10px; font-size: 16px; border-radius: 6px; border: 1px solid #334155; width: 80%; margin-bottom: 10px; background: #0f172a; color: white; }
    </style>
</head>
<body>
<div class="container">
    <?php if (!isset($_SESSION['logged_in']) || $_SESSION['logged_in'] !== true): ?>
        <div class="login-box">
            <h2>Авторизация</h2>
            <?php if (isset($error)) echo "<p class='error'>$error</p>"; ?>
            <form method="POST">
                <input type="password" name="password" placeholder="Введите пароль" required><br>
                <button type="submit">Войти</button>
            </form>
        </div>
    <?php else: ?>
        <div class="top-bar">
            <form method="GET" style="margin: 0;">
                <label for="file">Выберите плейлист: </label>
                <select name="file" id="file" onchange="this.form.submit()">
                    <?php foreach ($allowed_files as $key => $path): ?>
                        <option value="<?php echo $key; ?>" <?php if ($file_key === $key) echo 'selected'; ?>>
                            <?php echo htmlspecialchars($path); ?>
                        </option>
                    <?php endforeach; ?>
                </select>
            </form>
            <a href="?logout=1" style="color: #f87171; text-decoration: none; font-size: 14px;">Выйти</a>
        </div>
        
        <?php if ($message) echo "<p class='message'>$message</p>"; ?>
        
        <form method="POST">
            <textarea name="content"><?php echo htmlspecialchars($content); ?></textarea>
            <br>
            <button type="submit">Сохранить изменения</button>
        </form>
    <?php endif; ?>
</div>
</body>
</html>
