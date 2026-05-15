"""
PDF-ассемблер — сборка изображений в многостраничный PDF.

Использует Pillow для:
    - Конвертация RGBA/Palette → RGB
    - Сохранение многостраничного PDF
    - Настраиваемый DPI
"""

from pathlib import Path

from PIL import Image
from rich.console import Console

from .config import PDF_DPI

console = Console()


def build_pdf(image_paths: list[Path], output_path: Path) -> Path:
    """
    Собирает список изображений в один PDF-файл.

    Все изображения конвертируются в RGB (убирает проблемы с RGBA, palette, grayscale).
    Размер каждой страницы = размер соответствующего изображения.

    Args:
        image_paths: Отсортированный список путей к PNG/JPG файлам
        output_path: Путь для сохранения PDF

    Returns:
        Path: Путь к созданному PDF

    Raises:
        ValueError: если нет изображений
        RuntimeError: если ни одно изображение не открылось
    """
    if not image_paths:
        raise ValueError("❌ Нет изображений для сборки PDF")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    images: list[Image.Image] = []
    skipped = 0

    for p in image_paths:
        try:
            img = Image.open(p)

            # Обработка разных режимов
            if img.mode == "RGBA":
                # Создаём белый фон и накладываем изображение
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            images.append(img)

        except Exception as e:
            console.print(f"  ⚠️  Пропущен {p.name}: {e}")
            skipped += 1

    if not images:
        raise RuntimeError("❌ Ни одно изображение не удалось открыть")

    # Сохраняем многостраничный PDF
    first, rest = images[0], images[1:]
    first.save(
        str(output_path),
        format="PDF",
        save_all=True,
        append_images=rest,
        resolution=PDF_DPI,
    )

    # Статистика
    size_kb = output_path.stat().st_size / 1024
    size_str = f"{size_kb:.1f} КБ" if size_kb < 1024 else f"{size_kb / 1024:.1f} МБ"

    console.print(f"\n✅ PDF создан: [bold green]{output_path}[/bold green]")
    console.print(f"   📄 Страниц: {len(images)} | 📦 Размер: {size_str}")
    if skipped:
        console.print(f"   ⚠️  Пропущено: {skipped} файлов")

    return output_path
