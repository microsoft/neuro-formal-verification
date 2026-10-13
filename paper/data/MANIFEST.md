# nl2post-safe manifest

The 103 pairs of the benchmark, pinned to their upstream identifiers. Each row
identifies exactly one entry of the nl2post artifact, so the dataset can be
rebuilt and checked without any of our code.

- `postcondition_response_num` selects the record in `summary_eval_results.jsonl`
  and `raw_preprocessed_samples.jsonl` for that `task_id`.
- `mutant_response_num` selects the record in `distinct_code_mutants.jsonl`
  for that `task_id`. Values 0-199 are natural mutants, 200-299 artificial.
- `*_sha16` are the first 16 hex chars of the SHA-256 of the SANITIZED text, so a
  reproduction can be verified character-for-character.

| task_id | postcondition_response_num | mutant_response_num | entry_point | ref_sha16 | mut_sha16 | post_sha16 |
|---|---|---|---|---|---|---|
| HumanEval/1 | 2 | 0 | separate_paren_groups | 799e6e422a8b0090 | a92999dcabfb8e4c | bc4767cc6bc59a87 |
| HumanEval/2 | 0 | 202 | truncate_number | 6785b92bf5ebff14 | 0350f04fa32327f0 | fae973e69b836269 |
| HumanEval/3 | 0 | 200 | below_zero | ae37d5e423bca9ff | 1fa2c4d68062422d | 409c5f35a753606d |
| HumanEval/4 | 1 | 213 | mean_absolute_deviation | 308eb0639a985426 | 3711bbd99ef1b01f | 9972ce8484f40c9d |
| HumanEval/5 | 0 | 27 | intersperse | 9302dbddadf08217 | 691dc56e173bba24 | d0bc6f3f5167caa0 |
| HumanEval/6 | 1 | 7 | parse_nested_parens | de39e1e0e4f479f6 | aaf9ab64f47cc616 | 4e2794b14809e36e |
| HumanEval/8 | 0 | 49 | sum_product | 5abf77d093afcdd6 | 7a130ae28cb5c4bd | 74f5774b577fed17 |
| HumanEval/9 | 0 | 106 | rolling_max | 74e71dcde6697565 | 46c2ed6122aa5f0f | 823b0f07a479482e |
| HumanEval/10 | 0 | 0 | make_palindrome | 0cbbb622ee32e0e9 | 0aed8480d6112a89 | 07f32217c58050c2 |
| HumanEval/11 | 0 | 6 | string_xor | 5bbf0fa22d1ef4a3 | a3c41c7bd557c130 | ed46f83bc5ad8033 |
| HumanEval/13 | 0 | 205 | greatest_common_divisor | 863f443fafe5cf9d | 2dbfe77d565ccbee | ec79e4bdb48fb93b |
| HumanEval/17 | 2 | 202 | parse_music | b0d4a5dc3ec12169 | c3211bcb5f673d07 | bd35aa04c32f8356 |
| HumanEval/19 | 0 | 3 | sort_numbers | aa6325fe89386ec3 | e31cd9b04f903017 | bbb776a3f651ce13 |
| HumanEval/20 | 1 | 147 | find_closest_elements | fdfc6e1aa8b44cd6 | 9efded98371cbb17 | af8caac2674abb1c |
| HumanEval/21 | 1 | 200 | rescale_to_unit | 9cf43f69a636c754 | d72c7d1ab928f1dd | e5a876928652a2d9 |
| HumanEval/25 | 1 | 202 | factorize | 44994cba6d77476e | 4ad08c4a5108ff2e | b49d544600c55bb5 |
| HumanEval/26 | 0 | 205 | remove_duplicates | 1b03868652830ccd | 65d83592556ff76c | 9c75ff779d845eb6 |
| HumanEval/30 | 0 | 200 | get_positive | ea5481897188d63d | a18e0d6703f1e66d | 409bcdc210660cc5 |
| HumanEval/31 | 0 | 27 | is_prime | 54af735bfb571838 | 461d758d83d733ee | b504ee37795ba8a3 |
| HumanEval/33 | 0 | 5 | sort_third | 0bc4235b050c3454 | eda56b5a6dee1f21 | 8b19f607191cc0e5 |
| HumanEval/34 | 0 | 200 | unique | 16a1430bc36fd363 | 53279c7753b039d8 | 7f16137310fc8b0b |
| HumanEval/35 | 0 | 201 | max_element | f15a1d25e9660c21 | 1022141f2c19cbd8 | 63b21d7bc7871d04 |
| HumanEval/36 | 0 | 205 | fizz_buzz | a98fb66a8b56454a | e589fc304141bda4 | b44bad47618b5452 |
| HumanEval/37 | 5 | 11 | sort_even | 56ba1914b5297e2f | b1778b46456423b3 | 99d743b03c2670eb |
| HumanEval/41 | 0 | 7 | car_race_collision | 173eb8ed166dda7c | 3149588ab260aca2 | b44bad47618b5452 |
| HumanEval/42 | 0 | 200 | incr_list | b72fd56079ea67bb | 4a869a857c732d79 | 281be21a50aae0bd |
| HumanEval/43 | 5 | 42 | pairs_sum_to_zero | f5b15638ac0c8585 | 2fa21b66e65b3ff1 | 0dfaaaaac53ca064 |
| HumanEval/44 | 0 | 60 | change_base | 13c6b4a71eb8e125 | 50a5b2537cd3d401 | f160c36ddab1c015 |
| HumanEval/45 | 0 | 200 | triangle_area | c4d76b3bdaa09dec | 60670581c156675f | 0c7c261fc7e4f9a7 |
| HumanEval/47 | 4 | 200 | median | 928f82e6c093e201 | bdd031abd3d7acfa | 3948a76bb4fca487 |
| HumanEval/49 | 1 | 1 | modp | 10eac598f0ad1fd5 | 302d93d653ee93f0 | 004d0a04882bb782 |
| HumanEval/50 | 9 | 289 | decode_shift | 9d6ee653ecc1313c | 48111bbb580df582 | 5a9f0130553636f4 |
| HumanEval/51 | 0 | 200 | remove_vowels | fcc9fbae1762a829 | 515bb989efc13439 | 5c59f6d998174cec |
| HumanEval/53 | 0 | 200 | add | 8f29ed47ef356f6f | 8ddca5697d59d122 | a471e098f86185d5 |
| HumanEval/56 | 0 | 200 | correct_bracketing | e954482de204dd60 | 10b4f6f096428b0d | d7b99e1123400d13 |
| HumanEval/58 | 0 | 203 | common | d9c0e954d7ea3a5e | 5f9a40232e0c6d0d | 5d4818a864a7d6c2 |
| HumanEval/59 | 1 | 44 | largest_prime_factor | 8dd097a503798d77 | 67233f6b070ab2ce | 29fb515830ecb760 |
| HumanEval/60 | 0 | 189 | sum_to_n | 3e5edc31cd1eead8 | 4720757ec6887523 | 6cb02276a0e12294 |
| HumanEval/61 | 0 | 201 | correct_bracketing | b99fd0e9fcad0e37 | 3125a1915798cd09 | 76f08e4818f41ef6 |
| HumanEval/64 | 0 | 0 | vowels_count | 3f3cb6979b57d637 | b4dc4a18072d5f69 | dfddbe4ef83697f7 |
| HumanEval/65 | 0 | 7 | circular_shift | 57e121d0227ddf4a | e7fa24bb4f8cc365 | 9456e61ee5f62b0a |
| HumanEval/66 | 6 | 200 | digitSum | e80218215129fc35 | 6f6d3018a363b625 | 0d8963c6888ed452 |
| HumanEval/67 | 0 | 15 | fruit_distribution | 1aa661b4ac79745c | d91cdb964cada493 | d475f04e96c37816 |
| HumanEval/68 | 0 | 62 | pluck | 7431456047dda025 | 14ae29ce002c44de | 29d5368842ff0689 |
| HumanEval/69 | 1 | 1 | search | 2d8834bfc772a6f4 | b0fdad4de1a516ce | 0224fb631b9dc7e2 |
| HumanEval/70 | 0 | 48 | strange_sort_list | b4860e5cd530f478 | ed8ad922f37ceeb9 | 455e4291f0e5acf0 |
| HumanEval/71 | 0 | 210 | triangle_area | f690a14846afd99c | 22d49611e084ac1c | 60e814af4a0646bb |
| HumanEval/73 | 2 | 0 | smallest_change | 0361975a7f7883b8 | 72ef0eaa358b4e9a | f27b563678e5e009 |
| HumanEval/77 | 2 | 0 | iscube | 004a3fed2ca72b9a | 7c7b55a57cf3f8b9 | 0cbbd1dee7ae79c9 |
| HumanEval/78 | 1 | 203 | hex_key | f05aa5b1d3f1b2c2 | 28b0a398eb57e2dd | c0c1b2522a9584bb |
| HumanEval/79 | 0 | 6 | decimal_to_binary | 85d5871c81e292a1 | d85ff86051df9801 | e4dc3c639de92c48 |
| HumanEval/81 | 0 | 215 | numerical_letter_grade | 6a2d23f1dbbc1311 | 7aa49e032e55deef | 8e7c79bd7dfd12fc |
| HumanEval/86 | 2 | 0 | anti_shuffle | 27d124363316a003 | 1eb1457a69bd207f | 5a9f0130553636f4 |
| HumanEval/87 | 0 | 3 | get_row | 487f16d0ee0f3b01 | 6eb086d97702a9aa | d0cc1673eda603f9 |
| HumanEval/88 | 0 | 206 | sort_array | 7119b53c316c1c75 | 78ff2314b13e463f | ea4d96e5da2073da |
| HumanEval/89 | 0 | 3 | encrypt | 75f8005bd80b675e | 6fb725a8ee84d15b | 5a9f0130553636f4 |
| HumanEval/90 | 3 | 6 | next_smallest | 6faf14180c9db9f7 | 33be8de370fc50eb | b6f8a3ceb629d40d |
| HumanEval/93 | 1 | 17 | encode | e40f2a8d336afeca | ab66015ed4d518ae | 07726a11a82038e6 |
| HumanEval/96 | 3 | 1 | count_up_to | 9813f6da9553e23e | 76b169bf3faf8a5e | 8221d9af60c3ea0e |
| HumanEval/97 | 0 | 0 | multiply | 24156728d74b96aa | 09c3f8478e947ce0 | f04ba3737ca45f63 |
| HumanEval/99 | 9 | 9 | closest_integer | aca3df81fed30193 | fe9d03bee0570b69 | d05ae79dd0551f3b |
| HumanEval/100 | 1 | 0 | make_a_pile | 5d4a21b5317c36d9 | 6b23048a5f0b3f12 | bef27ab9908b8bf3 |
| HumanEval/102 | 0 | 5 | choose_num | 172b4f8c6b62c0af | 42c8e2b84c5a00dd | 794133428c155fbb |
| HumanEval/103 | 0 | 12 | rounded_avg | 01440d7346ffc741 | 368622c36d628944 | 1ec69370a7f72ff1 |
| HumanEval/104 | 0 | 126 | unique_digits | 7ba049a1bf929694 | 870089a23aba8191 | a3141b98df9c22b2 |
| HumanEval/105 | 1 | 203 | by_length | 7b95b8a62f9fa83a | e7fc67b3f60da624 | 2a7be95b4aa8bde3 |
| HumanEval/106 | 0 | 15 | f | 26d4309b38eb40ce | 23ab787de5b7a027 | 22abafc51f894a89 |
| HumanEval/107 | 0 | 140 | even_odd_palindrome | ffa390bab0812aa8 | d219b4b2c1fc7003 | ef521133bb245180 |
| HumanEval/108 | 2 | 59 | count_nums | 438f132fe314680a | 132340e941f80bb4 | 313efc3698cfd27a |
| HumanEval/110 | 4 | 0 | exchange | 9aaa3b6de864d2ca | ea64f2db4e6a4f68 | d3c47ffb55865892 |
| HumanEval/111 | 7 | 18 | histogram | 357761360d73a1a0 | a65bc03dcb7d1317 | 6d1783df21d864c3 |
| HumanEval/112 | 0 | 205 | reverse_delete | 10918be1add427a9 | e6f5ca6a79ca6a06 | a2cfc514eb5555d4 |
| HumanEval/113 | 0 | 220 | odd_count | 8db2f78325e3a06b | 5bc612085b4aef32 | 455e4291f0e5acf0 |
| HumanEval/114 | 4 | 0 | minSubArraySum | 98b4d24f4b67506b | 89c15b4c09720d4e | 322b2fa4e7335508 |
| HumanEval/115 | 0 | 0 | max_fill | 99325ba3325be035 | 7c54ad00cea05ff5 | b44bad47618b5452 |
| HumanEval/116 | 8 | 9 | sort_array | cf7e97fea4f4b04e | 91e0264e0132c19a | ee1ab58d6a83d84e |
| HumanEval/117 | 7 | 4 | select_words | 24fcbbd5c6e2fbdc | aac0e5b0c3eb9003 | 1e1eaf48f8dd9f6d |
| HumanEval/118 | 7 | 11 | get_closest_vowel | 23809fb54cd0a936 | 27a03ad3df888838 | f61549d54749da8b |
| HumanEval/120 | 0 | 0 | maximum | 5f04e82b5c0b046f | 2326700ebe33bec4 | c71c34b1345ff7fb |
| HumanEval/121 | 1 | 0 | solution | cac6b8be942281d5 | 90acd8a706427d71 | 1b9f098cfa23abc0 |
| HumanEval/123 | 0 | 43 | get_odd_collatz | 07aa3c6d8f2cedd0 | 57356b42a3b0676c | 9138cc415a83c9ed |
| HumanEval/127 | 0 | 235 | intersection | 306c01c5820aeb6b | 90397b76dd836f28 | 8c091d638605045b |
| HumanEval/128 | 2 | 77 | prod_signs | 407ffac07d66437c | b4ddfc68cce95cef | 425d8c46a5942d69 |
| HumanEval/130 | 0 | 3 | tri | c7d5cd40ac10204f | 9c2d1fc909902bff | f48d0865a5ec59ea |
| HumanEval/131 | 0 | 18 | digits | 1d832c0784d169bb | 721bc9cee4c22031 | e45faf06f1658c2f |
| HumanEval/133 | 0 | 151 | sum_squares | fbcc3ff0af835e21 | 5a5918d896730a5d | b44bad47618b5452 |
| HumanEval/135 | 0 | 0 | can_arrange | 28455f1d41543519 | 6822acef2ef23b56 | bf9e1dabf95f564d |
| HumanEval/136 | 0 | 115 | largest_smallest_integers | 99acb170f6e837e2 | b200275659d5ebdc | 1e74ce3f7def8407 |
| HumanEval/141 | 0 | 42 | file_name_check | 5cdb64243eb8fc16 | 9afb20b88d6c0bc4 | 97d512d1cf259d08 |
| HumanEval/143 | 1 | 200 | words_in_sentence | a498d2b813d0104d | a03ada55863494b9 | 658ad6ba5c97ded8 |
| HumanEval/145 | 3 | 134 | order_by_points | c9dab5b719b94e21 | a1e550fa00b49765 | a76e2929dea3217f |
| HumanEval/146 | 0 | 49 | specialFilter | 4c2269ff519eeff6 | 7f5c3b2641c0fa15 | 4eb790c5b17ffdfd |
| HumanEval/148 | 1 | 205 | bf | 780a79d00829f7c5 | eeb3c6068a955890 | 271b6bb5353bf6c9 |
| HumanEval/150 | 0 | 113 | x_or_y | a7f93b55badf69f0 | 1ed04a665563179c | 9ededc3395be6c3a |
| HumanEval/151 | 1 | 4 | double_the_difference | 9e919470ca78d38e | 69830a145b2d6df0 | b44bad47618b5452 |
| HumanEval/152 | 1 | 206 | compare | 99b6b3fb6ad872f0 | d31c0a76b092638f | 62a581accd96bc8f |
| HumanEval/153 | 1 | 4 | Strongest_Extension | ab8a25bb370213bc | 6c451ae6b5d31f62 | 157b532f47d236cc |
| HumanEval/155 | 0 | 8 | even_odd_count | c43cf415d63e442c | fb182f410863d149 | e619ba032240f95c |
| HumanEval/156 | 1 | 203 | int_to_mini_roman | 7f63b6d58edfba10 | bee5f1c76a9c68a1 | 999e9aca5c74cf36 |
| HumanEval/157 | 1 | 16 | right_angle_triangle | 5f6198dd6bd701ab | c71e72cd0401d194 | 7fd11fc9ebb584b7 |
| HumanEval/159 | 2 | 4 | eat | b2dbd0bbfeefa972 | 218153db5a88efd0 | 880a0ee475d172b6 |
| HumanEval/161 | 0 | 1 | solve | 41f028095977bd5f | e0f08a11c8400268 | 27a2ebce951f4982 |
| HumanEval/163 | 0 | 128 | generate_integers | 23be01a5a188b8f3 | 75728004097224dc | 89d8f69376109827 |
