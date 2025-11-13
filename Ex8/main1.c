#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <string.h>

#define NUMBERS 7
#define MAX_NUMBER 69
#define MIN_GROUPS 5
#define MAX_LINES 100

int main() {
    int n;
    scanf("%d", &n);
    srand(1);

    FILE *fp = fopen("lotto.txt", "w");
    if (!fp) return 1;

    int total_groups = (n < 5) ? 5 : n;

    fprintf(fp, "========= lotto649 =========\n");
    fprintf(fp, "===== November 13 2025 =====\n");

    for (int i = 0; i < total_groups; i++) {
        fprintf(fp, "[%d]: ", i + 1);

        if (i < n) {
            int numbers[NUMBERS] = {0};
            for (int j = 0; j < NUMBERS; j++) {
                int num, unique;
                do {
                    unique = 1;
                    num = rand() % MAX_NUMBER + 1;
                    for (int k = 0; k < j; k++) {
                        if (numbers[k] == num) {
                            unique = 0;
                            break;
                        }
                    }
                } while (!unique);
                numbers[j] = num;
            }

            for (int j = 0; j < NUMBERS; j++) {
                fprintf(fp, "%02d", numbers[j]);
                if (j < NUMBERS - 1) fprintf(fp, " ");
            }
        } else {
            fprintf(fp, "__ __ __ __ __ __ __");
        }

        fprintf(fp, "\n");
    }

    fprintf(fp, "========= csie@CGU =========\n");
    fclose(fp);

    FILE *fp2 = fopen("lotto.txt", "r");
    if (!fp2) {
        printf("無法開啟 lotto.txt\n");
        return 1;
    }

    int winning_number[3];
    printf("請輸入中獎號碼三個: ");
    for (int i = 0; i < 3; i++) {
        scanf("%d", &winning_number[i]);
    }

    printf("輸入的中獎號碼為：");
    for (int i = 0; i < 3; i++) {
        printf("%02d ", winning_number[i]);
    }
    printf("\n");
    printf("以下為中獎彩券:\n");

    char lines[MAX_LINES][256];
    int lotto_numbers[MAX_LINES][NUMBERS];
    int line_count = 0;
    char line[256];

    while (fgets(line, sizeof(line), fp2)) {
        if (line[0] == '[') {
            strcpy(lines[line_count], line);
            int num_count = sscanf(line, "[%*d]: %d %d %d %d %d %d %d",
                                   &lotto_numbers[line_count][0], &lotto_numbers[line_count][1],
                                   &lotto_numbers[line_count][2], &lotto_numbers[line_count][3],
                                   &lotto_numbers[line_count][4], &lotto_numbers[line_count][5],
                                   &lotto_numbers[line_count][6]);
            if (num_count == NUMBERS)
                line_count++;
        }
    }
    fclose(fp2);

    int found = 0;
    for (int i = 0; i < line_count; i++) {
        int match = 0;
        for (int j = 0; j < NUMBERS; j++) {
            for (int k = 0; k < 3; k++) {
                if (lotto_numbers[i][j] == winning_number[k]) {
                    match = 1;
                    break;
                }
            }
            if (match) break;
        }
        if (match) {
            printf("售出時間:%s", lines[i]);
            found = 1;
        }
    }

    if (!found)
        printf("沒有中獎\n");

    return 0;
}
