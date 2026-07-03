#!/usr/bin/env python3
"""
生成 Markdown 格式的九九乘法表
"""

def generate_multiplication_table():
    """生成九九乘法表的 Markdown 表格内容"""
    lines = []
    
    # 添加标题
    lines.append("# 九九乘法表\n")
    
    # 添加 Markdown 表格头部
    header = "| "
    separator = "| "
    for i in range(1, 10):
        header += f" {i} |"
        separator += " --- |"
    lines.append(header)
    lines.append(separator)
    
    # 添加表格内容
    for i in range(1, 10):
        row = f"| **{i}** |"
        for j in range(1, 10):
            if j >= i:
                row += f" {i}×{j}={i*j:2d} |"
            else:
                row += "     |"
        lines.append(row)
    
    return "\n".join(lines)

def save_to_file(content, filename="九九乘法表.md"):
    """将内容写入文件"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"✅ 已生成文件: {filename}")

if __name__ == "__main__":
    content = generate_multiplication_table()
    print(content)
    save_to_file(content)
