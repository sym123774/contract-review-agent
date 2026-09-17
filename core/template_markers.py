"""Mark template instructions and unselected alternatives without deleting source text."""
import re


INSTRUCTION_PATTERN = re.compile(
    r"示范文本|填写说明|使用说明|签订本合同前|本合同文本供|请根据实际|"
    r"选择使用|可选择以下|以下条款供选择"
)
BLANK_SELECTOR_PATTERN = re.compile(r"(?:以下|下列|按).{0,20}第?[_＿]{2,}项")
OPTION_PREFIX = re.compile(r"^[（(]?[一二三四五六七八九十\d]+[)）、.．]")
UNCHECKED_BOX = re.compile(r"(?:^|\s)[□☐○](?!\s*[√✓☑☒])")
FULL_PAREN_INSTRUCTION = re.compile(r"^[（(][\s\S]{20,}[）)]$")
INSTRUCTIONAL_WORDING = re.compile(
    r"是指|应当|应(?:在|与|包括|明确|注明|载明)|可以由|可由|可以在.{0,10}约定|供.{0,8}(?:填写|选择)"
)


def mark_template_clauses(clauses):
    """Annotate clauses in place with active/instruction/unselected_option/template_blank."""
    statuses = ["active"] * len(clauses)
    for index, clause in enumerate(clauses):
        text = (clause.get("title", "") + "\n" + clause.get("content", "")).strip()
        content = (clause.get("content", "") or "").strip()
        if (INSTRUCTION_PATTERN.search(text) or
                (FULL_PAREN_INSTRUCTION.match(content) and INSTRUCTIONAL_WORDING.search(content))):
            statuses[index] = "instruction"
        elif UNCHECKED_BOX.search(text) and not re.search(r"[√✓☑☒]", text):
            statuses[index] = "unselected_option"
        elif re.search(r"_{3,}|＿{3,}", text):
            statuses[index] = "template_blank"

    for index, clause in enumerate(clauses):
        text = (clause.get("title", "") + "\n" + clause.get("content", "")).strip()
        if not BLANK_SELECTOR_PATTERN.search(text):
            continue
        for option_index in range(index + 1, len(clauses)):
            option = clauses[option_index]
            option_text = (option.get("title", "") or option.get("content", "")).lstrip()
            if OPTION_PREFIX.match(option_text):
                statuses[option_index] = "unselected_option"
            else:
                break

    for clause, status in zip(clauses, statuses):
        clause["template_status"] = status
    return clauses
